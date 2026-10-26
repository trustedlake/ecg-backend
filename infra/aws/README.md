# infra/aws: ECG backend production

Terraform for the architecture in `docs/migration-plan-v1.md` §0. State in
`s3://ecg-terraform-state-785236377332-us-east-1/ecg-backend-prod/terraform.tfstate` (`us-east-1`; the old sa-east-1 state stays in `ecg-lambdas-terraform-state` until the stack is destroyed).
Buckets and lambdas are **not** managed here (they come from the `trustedlake/ecg-lambdas-v1` stacks: `ecg-reading`, `ecg-interpretation`).

## Prerequisites
- Terraform >= 1.11 (write-only/ephemeral); provider `hashicorp/aws` ~> 6.69.
- Credentials for account `785236377332` (profile `trustedlake` by default; `-var aws_profile=` to change it).
- **SecureString** parameters created by hand (no values in git):

  | Name | Use |
  |---|---|
  | `/ecg/prod/DB_PASSWORD` | password of the RDS master user `ecg_admin` (read by Terraform and by the task) |
  | `/ecg/prod/DJANGO_SECRET_KEY` | Django `SECRET_KEY` |
  | `/ecg/prod/ADMIN_USERNAME` | superuser created by `manage.py ensure_admin` |
  | `/ecg/prod/ADMIN_PASSWORD` | that superuser's password |

  ```sh
  aws ssm put-parameter --region us-east-1 --type SecureString --name /ecg/prod/DB_PASSWORD --value '<password>'
  ```
  The RDS password cannot contain `/`, `@`, `"` or spaces.

## Commands
```sh
cd infra/aws
terraform init
terraform plan -out tf.plan  # default alarm_email: gustavo.miler@trustedlake.com
terraform apply tf.plan      # only with explicit approval
```
The database password never goes into the plan or the state (`password_wo`). To change it: update the
SSM parameter and run with `-var db_password_version=<n+1>`.

After the first apply, confirm the SNS subscription from the email you receive (without it alarms are not delivered).

## What each file creates
- `network.tf`, `rds.tf`, `ecr.tf`, `sqs.tf`, `iam.tf` (I1): VPC, SGs, RDS, ECR, queue + DLQ, task roles.
- `ecs.tf`: cluster `ecg`, task `ecg-backend` (containers `web` and `worker`), service and Cloud Map `backend.ecg.internal`.
- `api.tf`: HTTP API -> VPC Link -> Cloud Map. The URL is in the `api_url` output.
- `schedule.tf`: autoscaling (1-2 tasks, CPU 70 %) and business hours.
- `monitoring.tf`: SNS `ecg-alerts`, 10 alarms and the monthly budget (US$35).
- `cicd.tf`: GitHub connection, 3 CodeBuild projects, artifact bucket and the CodePipeline.

## Business hours
Mon-Fri, in the timezone set in `schedule.tf`: RDS starts at 07:40 and stops at 20:10; ECS scales up at 08:00 and
goes to 0 at 20:00. Outside those hours API Gateway answers 503 and the app shows the business-hours notice.

- One-off exception: `scripts/start.sh` (RDS, then ECS min 1/max 2) and `scripts/stop.sh` (the reverse).
  They last until the next scheduled action.
- Always on: `-var office_hours_enabled=false` removes the schedules (cost about US$60/month). Then
  run `scripts/start.sh` once, because Terraform does not touch the current autoscaling min/max.
- The times are in the variables `ecs_start_cron`, `ecs_stop_cron`, `rds_start_cron` and `rds_stop_cron`.

## Task size
`task_cpu` (default 512 = 0.5 vCPU) and `task_memory` (1024 MB). For 1 vCPU: `-var task_cpu=1024 -var
task_memory=2048`. Terraform registers a new task definition revision; the service only uses it
on the next pipeline deploy (the service ignores `task_definition`).

## CI/CD (`cicd.tf`, `ci/`)
CodePipeline V2 `ecg-backend` (QUEUED mode): a push to `main` of `trustedlake/ecg-backend` **or**
`trustedlake/ecg-reader` -> **Test** (`ci/buildspec-test.yml`) and **Build** (Expo web export with
`scripts/build_frontend.sh` + ARM image in ECR, tag `<back sha>-<front sha>`) in parallel -> **Deploy** (`ci/deploy.sh`).
Timings, optimizations and cost per deploy: `docs/deploy-pipeline.md` (`scripts/pipeline_timing.sh` measures them).
A pipeline failure sends an email through the SNS topic `ecg-alerts`.

1. **Authorize GitHub (once, after the apply):** in us-east-1 Developer Tools -> Settings -> Connections,
   authorize `ecg-github-frontend` as `trustedlake` for backend, frontend and Lambdas.
   All production sources use this connection. `ecg-github` is retained for rollback only.
2. **Trigger by hand:** `aws codepipeline start-pipeline-execution --name ecg-backend`.
3. **What the deploy does:** starts RDS if it is stopped, registers a new task definition revision
   (from the latest one in the family, so Terraform changes land here), runs `migrate` and
   `ensure_admin` in a single one-off task, updates the service, waits for the new task, smoke-tests `/api/health/` (new version) and `/`,
   and rolls back to the previous revision if the smoke test fails.
4. **Deploy outside business hours:** the environment is turned on only for the deploy and turned off again at the end
   (ECS at 0, RDS stopping). Within business hours it stays as it is.

## First deploy
The service and autoscaling start with 0 tasks, because the `bootstrap` image does not exist in ECR. The first
pipeline run creates the image and the revision and starts the task (or the 08:00 action starts it the next day).

## Region: us-east-1 (moved from sa-east-1 on 2026-10-23)
The whole platform runs in `us-east-1` (the competition is evaluated there), with a new empty database and empty buckets.

| Item | Value |
|---|---|
| API URL | `https://7y0j5a8yte.execute-api.us-east-1.amazonaws.com` |
| Terraform state bucket | `ecg-terraform-state-785236377332-us-east-1` (versioned, encrypted) |
| State keys | `ecg-backend-prod/terraform.tfstate` (this stack), `prod/terraform.tfstate` (`ecg-lambdas`, buckets only), `ecg-mock-lambdas/terraform.tfstate` (`ecg-lambdas-v1/mock_lambdas`, Lambdas `ecg-reading` and `ecg-interpretation`) |
| Buckets | `ecg-lambdas-images-785236377332-us-east-1`, `ecg-lambdas-results-785236377332-us-east-1` |
| Reading image | ECR `ecg-analyser`, deployed by the Lambdas pipeline (tag = commit sha7). `v6-quiet` was copied from sa-east-1 with a one-off CodeBuild pull/tag/push; `v7-traced-fallback` and `v8-tips` were pushed by hand |
| Models bucket | `ecg-lambdas-models-785236377332-us-east-1` (versioned, **created by hand**; holds `model_selector.pkl`) |
| Budget | `ecg-prod-monthly-us-east-1` (budget names are account-global) |

Names that are global in the account carry the region suffix (IAM roles `ecg-*-us-east-1`, pipeline artifact bucket
`ecg-backend-pipeline-<account>-us-east-1`, budget) so that the old sa-east-1 stack can coexist until it is destroyed.
The CodeStar connections (`ecg-github`, `ecg-github-frontend`) are regional and must be authorized in the console of this region.

The old sa-east-1 stack is **stopped, not destroyed**: state in `s3://ecg-lambdas-terraform-state` (keys `ecg-backend-prod/`,
`ecg-mock-lambdas/`, `prod/`), ECS at 0 tasks, RDS stopped (AWS restarts a stopped RDS after 7 days), pipeline disabled.

### Lambdas pipeline (`ecg-lambdas`, us-east-1)
Code of `ecg-reading` and `ecg-interpretation` is deployed by CodePipeline `ecg-lambdas`, defined in
`ecg-lambdas-v1/lambdas/infra`. Source is `trustedlake/ecg-lambdas-v1` on `main` (connection `ecg-github-frontend`); Test and Build
run in parallel, then Deploy updates both functions, sets `CODE_VERSION` and runs `smoke_aws.py`. If the smoke test fails
the previous code is restored. There is no manual approval. Failures reach `ecg-alerts` through the EventBridge rule
`ecg-lambdas-pipeline-failed`; this stack's SNS topic policy (`monitoring.tf`) allows that rule to publish.
Details are in the `ecg-lambdas-v1` README (`mock_lambdas/README.md`).

### sa-east-1 shutdown state (2026-10-23)
- ECS service `ecg-backend`: desired 0, autoscaling min/max 0, scheduled scaling suspended.
- RDS `ecg`: `stopped`; the `ecg-rds-start`/`ecg-rds-stop` schedules are DISABLED. **AWS restarts it on 2026-10-30**: destroy
  the stack before that date or stop it again.
- Pipeline `ecg-backend` (sa-east-1): the transition into `TestAndBuild` is disabled, so merges do not deploy there.
- `aws_ce_cost_allocation_tag.project` is account-global: run `terraform state rm` on it in the old state before `destroy`.

### Validation in us-east-1 (2026-10-23)
`/api/health/` ok (db ok), `/` serves the HTML, admin login works, the presigned PUT goes to the new images bucket, the worker
reads from the new queue and `ecg-reading` writes to the new results bucket, `ecg-interpretation` returns `OK_WITH_WARNINGS`.
The sample image is refused by the reading (`READ_REFUSED_UNREADABLE_TRACE`), so the 6-method view still needs a real ECG photo.
Alarms: 9/10 OK; `ecg-rds-cpu-credits` stays in ALARM for a few hours until the new RDS earns credits.
