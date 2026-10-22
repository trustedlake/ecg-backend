# Deploy pipeline: measurements, optimizations and the Fargate-task decision

Card: "Shorten deploy pipeline (9 min -> 3-5 min) and review the 2 Fargate tasks" (AWS review, 2026-10-21).
Target: 3-5 min per deploy. Everything below is reproducible with `scripts/pipeline_timing.sh`.

## 1. Before (measured, 7 successful runs on 2026-10-20, region sa-east-1)

| Stage / action | Warm (RDS up) | Off-hours (RDS stopped) |
|---|---|---|
| Source (2 repos) | 5-7 s | 5-7 s |
| Test (CodeBuild ARM small) | 32 s | 32 s |
| Build (Expo export + image + ECR push) | 62-216 s (typ. 125-155 s) | 124-155 s |
| **Deploy (CodeBuild waiting on ECS)** | **585-617 s** | **923-954 s** |
| **Pipeline total** (incl. stage hand-offs) | **12.5-14.6 min** | **18-19 min** |

The "9 min" quoted in the review is the Deploy stage (~10 min). Breakdown of one Deploy (CodeBuild log + ECS events):

| Step | Time | Why |
|---|---|---|
| RDS start (off-hours only) | ~6 min | RDS boot, inherent |
| `migrate` one-off task | **3:05** | `manage.py migrate` itself runs in < 1 s (log at +17 s). The task then idles until it is stopped: the idle `worker` container runs `signal.pause()` as PID 1, which **ignores SIGTERM**, so ECS waits out the worker `stopTimeout` (120 s) |
| `ensure_admin` one-off task | **3:05** | Same: command < 1 s, ~3 min of waiting |
| Service rollout | **3:12** | new task RUNNING ~58 s after RunTask, healthy after the 30 s health-check interval (~70-80 s), then ~100 s until ECS reports "deployment completed" (old task SIGTERM -> worker long poll up to 20 s -> Fargate teardown) |
| smoke test | ~5 s | |

So ~6 of the ~10 minutes were pure waiting on a container that did nothing.

## 2. Changes

| Change | Where | Saves (warm) |
|---|---|---|
| One one-off task running `migrate && ensure_admin` instead of two | `ci/deploy.sh` | ~35 s per extra task start |
| Idle `worker` of the one-off task exits on SIGTERM (`signal.signal(SIGTERM, sys.exit)`) | `ci/deploy.sh` | ~2.5 min per one-off task (5 min -> ~35 s for the one-off task) |
| Wait for the new task to be RUNNING + smoke test, not for "deployment COMPLETED" (old-task teardown). The smoke test still gates the deploy, a FAILED rollout is still detected and a failed smoke test still rolls back | `ci/deploy.sh` | ~1.5 min |
| Web container health check `interval` 30 s -> 5 s, `startPeriod` 60 -> 30 s (gunicorn boots in seconds) | `infra/aws/ecs.tf` | ~25 s |
| Test and Build run in parallel (one `TestAndBuild` stage, same `run_order`); Deploy still needs both | `infra/aws/cicd.tf` | ~30-90 s (the Test duration) |
| Off-hours: Test action pre-starts RDS (so its ~6 min boot overlaps with test+build); `update-service` before raising the autoscaling minimum (avoids a wasted old-revision task) | `ci/buildspec-test.yml`, `ci/deploy.sh`, `infra/aws/cicd.tf` (least-privilege `rds:StartDBInstance`/`DescribeDBInstances` on the one instance) | ~2 min off-hours |
| Local CodeBuild cache (pip, npm, Docker layers) on test/build | `infra/aws/cicd.tf` | few seconds, best effort (see below) |

Deliberately **not** changed:

- **Machine size.** CodeBuild is billed per started minute; build is ~2 min of which Expo bundling is 15 s and the Docker build ~17 s, so a
  medium machine (2x price) would save well under a minute of a billed minute. The deploy project only runs the AWS CLI. Keep ARM small.
- **Cache is a small win.** Local cache only helps when a build lands on a still-warm host; with a few runs a day it rarely does. S3 cache would add up/download time
  comparable to what it saves (pip install 8 s, `npm ci` 9 s). It is kept because it is free.
- **Deregistration delay.** There is no load balancer: API Gateway reaches the task through a VPC Link + Cloud Map (SRV TTL 10 s), so there is no ALB
  deregistration delay to tune. The web container stops in ~1 s; the worker keeps `stopTimeout=120` on purpose (it may be mid-reading, 50-190 s).
- **Insurance link check (`ensure_admin`) and the health checks** are all kept (container health check, `/api/health/` version check, `/` HTML check).

## 3. Decision: should the two Fargate tasks be merged?

Original: task 1 = `migrate`, task 2 = `ensure_admin` (and then the service's own task). Options considered:

| Option | Rollback | Concurrency | Verdict |
|---|---|---|---|
| A. Migrate in the container entrypoint | The old task is already serving when the new task migrates; a failed/slow migration makes the new task unhealthy (circuit breaker rolls back the code, **not the schema**); old code runs on the new schema the whole time | `web` and `worker` are two containers of the same task, and during a rolling deploy plus autoscaling (max 2 tasks) several tasks start: migrations would race (needs advisory locks) and every scale-out would re-run them | Rejected |
| B. One one-off task before `update-service` (**chosen**) | A failed migration aborts the deploy and the running service is untouched | Exactly one migration run per deploy | **Chosen.** Merged `migrate` and `ensure_admin` into a single task: -1 task start |
| C. Run migrate inside the CodeBuild deploy job | Same as B | Same as B | Needs CodeBuild in the VPC with the DB secret/SG access (more infra, more risk) to save ~30 s |

The extra cost was never the task, it was the 3-minute idle wait; both fixes are in. Migrations must stay backward compatible with the
previous code (expand/contract), since the old task serves while the new one starts.

## 4. After and cost per deploy

Estimated from the changes above (CodeBuild bills per started minute; to be replaced with measured numbers after the first runs):

| | Before (warm) | After (warm, estimate) |
|---|---|---|
| Pipeline total | ~13 min (12.5-14.6) | ~4.5-5 min |
| Deploy stage | ~10 min | ~2-2.5 min |
| CodeBuild minutes (test + build + deploy, billed) | 1 + 3 + 10 = 14 | 1 + 2-3 + 3 = 6-7 |
| CodeBuild + CodePipeline cost | ~US$0.10-0.11 | ~US$0.05 |
| Fargate one-off tasks (0.5 vCPU / 1 GB, US$0.000566/min) | 2 x ~3.1 min = US$0.0035 | 1 x ~0.6 min = US$0.0003 |
| Overlap of old+new service task during rollout | ~3 min, US$0.0017 | ~1 min, US$0.0006 |
| **Total per deploy** | **~US$0.11** | **~US$0.05** |

Off-hours deploys (RDS stopped): 18-19 min before; RDS boot (~6 min) now overlaps test+build, so ~8-9 min expected.

**Record the measured "after" here** (run `scripts/pipeline_timing.sh 5` after the first deploys with the new pipeline):

| Run | Total | TestAndBuild | Deploy | Cost |
|---|---|---|---|---|
| _pending first runs after merge + `terraform apply`_ | | | | |

## 5. Rollout order

1. `terraform apply` in `infra/aws` (pipeline structure, CodeBuild caches, test-role RDS permission, task-definition health check). The plan also shows
   unrelated pending description-only changes (English translations of alarm/SG/ECR-policy descriptions).
2. Merge to `main`. A merge in this repo **or** in ecg-reader triggers a full deploy.
3. The first deploy after merge runs `ci/deploy.sh` from the new commit, so it already includes the script changes; the new health-check settings land with the
   task-definition revision registered from the latest family revision (step 1 must be applied first).
