# Execution plan: front/back/infra migration to Expo Go + AWS CI/CD (V1)

> Written on 2026-10-15 to be executed **step by step** (e.g. with Sonnet 5.5). Each step is a self-contained
> prompt: copy the whole "Prompt" block into a new session. Wait for one step's report before running the
> next (the steps depend on what was actually implemented, not on what was planned).
>
> Decisions and user flow: [`user-flow-v1.md`](user-flow-v1.md) (required reading for every step).
> Lambda contract: the lambda contract doc in the tl-gmiler/ecg-lambdas-v1 repo.

## 0. Shared context (applies to every step)

**Repositories (on this machine):**
- Backend: `/Users/gustavomiler/workspace/trabalho/projeto-ecg/ecg-backend` (Django 5.2 + DRF, token auth).
  This card's work is on the branch `tlk/card-58ed1492-bf5c-431f-b4ae-dfe6787488c9-5a5c322e99`
  (the card worktree), which already has the lambda contract doc and `mock_lambdas/`.
- Frontend: `/Users/gustavomiler/workspace/trabalho/projeto-ecg/ecg-reader` (npm workspaces monorepo:
  `apps/mobile` = Expo SDK 57 / RN 0.86 / React 19, `packages/shared` = components + `createApiClient`,
  `apps/web` = legacy Vite, to be retired). Before writing Expo code, read the versioned docs
  `https://docs.expo.dev/versions/v57.0.0/` (rule from `apps/mobile/AGENTS.md`). Work on the branch
  `feat/expo-v1-flow` (create it from `main`).
- Lambdas: ~~`ecg-reading-mock` and `ecg-interpretation-mock`~~ -> since B6, `ecg-reading` (real, contract 2.0) and
  `ecg-interpretation` (mock), in `sa-east-1` (account `785236377332`); test scenarios through the `mock` field.
  Buckets: images `ecg-lambdas-images-785236377332-sa-east-1`, results `ecg-lambdas-results-785236377332-sa-east-1`
  (managed by the Terraform of the `ecg-lambdas` repo, **not** by this repo).

**Target architecture (decided on 2026-10-15, not up for debate):**
```
Expo Go (phone) ───┐
Browser (web) ─────┴─HTTPS─▶ API Gateway HTTP API (execute-api URL, throttling, access log)
                               └─ VPC Link ─▶ Cloud Map (private DNS) ─▶ ECS Fargate ARM (ecg-backend service)
                                                                         task: "web" container (gunicorn, Django + WhiteNoise:
                                                                               /api/*, /admin/ and the Expo web export)
                                                                             + "worker" container (manage.py ecg_worker,
                                                                               consumes the SQS queue)
  web ──▶ SQS ecg-tasks (+ DLQ) ──▶ worker ──▶ Lambda ecg-reading (2.0) / ecg-interpretation (mock)
  web/worker ──▶ RDS PostgreSQL db.t4g.micro Single-AZ (private subnet, TLS)
  web/worker ──▶ S3 (presigned PUT for the photo, GET for the artifacts)
  secrets: SSM Parameter Store SecureString /ecg/prod/* injected by ECS (task definition "secrets")
Network: own VPC, 2 public subnets (Fargate tasks with a public IP for egress only; the SG only accepts ingress from the
      VPC Link SG) + 2 private subnets with no internet route (RDS). No NAT.
Schedule: EventBridge Scheduler (Mon-Fri, schedule timezone) starts RDS at 07:40 and ECS at 08:00; stops ECS at
      20:00 and RDS at 20:10. Outside that the URL returns 503.
CI/CD: CodePipeline V2 (GitHub: ecg-backend + ecg-reader) -> CodeBuild ARM test -> build (image in ECR) ->
      deploy (migrate in a one-off task -> update-service with circuit breaker -> smoke -> rollback on failure)
Observability: CloudWatch Logs (14 days), 10 alarms -> SNS ecg-alerts (email), AWS Budget US$35/month
```
Forbidden: CloudFront, Amplify Hosting, ALB/NLB, NAT Gateway, hand-managed EC2, Aurora, RDS Multi-AZ,
VPC (interface) endpoints, Elastic IP, Container Insights, Secrets Manager (use SSM). Cost target:
about US$25/month with the nightly shutdown (estimate in §0.2).

**Rules for whoever executes:**
- No `terraform apply`, deploy, `aws ... create/update/delete` or changes to real resources without
  Gus's explicit confirmation for that step. `terraform plan` and read-only AWS calls are allowed.
- Do not commit secrets. Do not push or open a PR without asking.
- Small commits, English messages in the `feat: ...` / `fix: ...` / `chore: ...` style.
- A behavior change without a test covering it = step failed.
- At the end of every step, return the **report** asked for at the end of the prompt.

### 0.1 Backend API contract (V1): source of truth for back and front

Authentication: `Authorization: Token <token>` (DRF). Validation errors: 400 with the default DRF format.
State errors: 409 `{"detail": "...", "code": "invalid_state"}`.

| Method and route | Auth | Body | Response |
|---|---|---|---|
| `GET /api/health/` | no | n/a | `200 {"status":"ok","db":"ok","version":"<git sha>"}` (503 if the database fails) |
| `POST /api/auth/login/` | no | `{username, password}` | `{token, user:{id, username, is_staff}}` (already exists; add `is_staff`) |
| `GET /api/config/` | yes | n/a | `{layouts:[...6], calibration:{speed_mm_s:[12.5,25,50], gain_mm_mv:[5,10,20], default:{speed_mm_s:25, gain_mm_mv:10}}, test_scenarios_enabled: bool}` (`true` only if the flag is on **and** the user is staff) |
| `GET /api/exams/` | yes | n/a | paginated, each item = a summarized `Exam` |
| `POST /api/exams/` | yes | `{patient_id}` **or** `{name, age, sex:"F"\|"M"}`, `reason?` | `201 Exam` |
| `GET /api/exams/<exam_id>/` | yes | n/a | `Exam` with `attempts[]` (each one = a full `Attempt`) |
| `POST /api/exams/<exam_id>/attempts/` | yes | `{filename, content_type: image/jpeg\|image/png\|image/webp, layout, speed_mm_s?, gain_mm_mv?, test_scenario?}` | `201 {attempt_id, upload_url, upload_headers:{"Content-Type": ...}}` |
| `POST /api/exams/<exam_id>/attempts/<id>/read/` | yes | n/a | `202 Attempt` (`reading`). 409 if not in `created` |
| `GET /api/exams/<exam_id>/attempts/<id>/` | yes | n/a | `Attempt` (the polling endpoint) |
| `POST .../attempts/<id>/accept/` | yes | n/a | `202 Attempt` (`interpreting`). 409 if not in `awaiting_validation` |
| `POST .../attempts/<id>/reject/` | yes | n/a | `200 Attempt` (`user_rejected`). 409 likewise |
| `POST .../attempts/<id>/retry/` | yes | n/a | `202 Attempt`. Only in `reading_error`/`interpretation_error` with `retryable=true`; otherwise 409 |
| `POST /api/attempts/<id>/feedback/` | yes | `{issues[], description}` | already exists, keep it |

`test_scenario` (optional): `{"read": {"scenario": "success|success_with_warnings|failure|refused", "code"?: "..."},
"interpret": {"scenario": "success|success_with_warnings|failure", "code"?: "..."}}`. Accepted only if
`ECG_TEST_SCENARIOS_ENABLED=true` **and** `request.user.is_staff`; otherwise 400. It is forwarded as `mock` in the
payload of the matching lambda.

`Attempt` object:
```json
{
  "id": "uuid", "exam_id": "uuid", "status": "<state from flow section 3>",
  "layout": "3x4+1", "calibration": {"speed_mm_s": 25, "gain_mm_mv": 10},
  "original_image_url": "presigned GET | null",
  "reading": null | {
    "status": "OK|OK_WITH_WARNINGS|REFUSED|ERROR",
    "warnings": [{"code","message","lead?"}],
    "error": null | {"code","category","retryable","message"},
    "overlay_url": "presigned GET | null",
    "refusal_preview_url": "presigned GET | null",
    "refusal_region_px": [x0,y0,x1,y1] | null,
    "leads": [{"name","kind","quality","coverage"}],
    "methods": [{"method","status","selector_score","chosen","image_url": "presigned GET | null"}]  // the six, registry order
  },
  "interpretation": null | {
    "status": "OK|OK_WITH_WARNINGS|ERROR",
    "warnings": [...], "error": null | {...},
    "summary": {...contract 3.2...} | null, "details": {...} | null, "disclaimer": "..." | null
  },
  "can_retry": bool, "needs_new_photo": bool,
  "created_at": "...", "updated_at": "..."
}
```
Presigned URLs are generated on every response and **never** stored in the database. Signal samples **never** go to
the app (they stay in S3, `artifacts.signal`).

### 0.2 Cost estimate (`sa-east-1` prices from the AWS pricing API, 2026-10-15, before taxes)

On-hours: Mon-Fri 08:00-20:00, about 264 h/month.

| Item | On only during business hours | 24/7 (reference) |
|---|---|---|
| Fargate ARM 0.5 vCPU / 1 GB (US$0.0557/vCPU-h + US$0.00612/GB-h) | $8.97 | $24.80 |
| Task public IPv4 (US$0.005/h) | $1.32 | $3.65 |
| RDS `db.t4g.micro` Single-AZ (US$0.034/h) | $8.98 | $24.82 |
| RDS gp3 20 GB (US$0.219/GB-month, billed even when stopped) | $4.38 | $4.38 |
| Cloud Map private DNS (Route 53 zone) | $0.50 | $0.50 |
| API Gateway HTTP API (US$1.59/million), ECR, Logs, SQS, Scheduler, S3, mock lambdas | ~$1.10 | ~$1.10 |
| CodePipeline V2 + CodeBuild ARM (100 free min/month each) | ~$0-1 | ~$0-1 |
| **Total** | **about US$25-26** | **about US$59-60** |

`task_cpu` variable: with 256 (0.25 vCPU) Fargate drops to ~$5.29 (total about US$22).

---

## Order and dependencies

| Step | Repo | Depends on | Can run in parallel with |
|---|---|---|---|
| B1 Models, states and validation | backend | n/a | F1 |
| B2 Lambda contract client | backend | B1 | F1 |
| B3 Async tasks + views/routes | backend | B2 | F1, F2 |
| B4 Container (Docker), SQS worker, prod settings, SPA | backend | B3 | F2, F3 |
| F1 API client and types (`shared`) | frontend | §0.1 | B1-B3 |
| F2 Screens: login -> exam -> photo -> layout -> processing | frontend | F1 | B3, B4 |
| F3 Screens: validation, refusal, error, result, history | frontend | F2 | B4 |
| F4 Web export served by Django + local end-to-end check | both | B4, F3 | n/a |
| I1 Terraform: network, RDS, ECR, SQS, IAM (plan only) | backend | B4 | F2-F4 |
| I2 Terraform: ECS, API Gateway, schedule, monitoring (plan only) | backend | I1 | F2-F4 |
| I3 CI/CD: CodePipeline V2 + CodeBuild (plan only) | backend | I2, F4 | n/a |
| B6 Reading on contract 2.0 (`ecg-reading`) + merge of `main` | both | I3, F4 | n/a |
| D1 First prod deploy (**with Gus**) | n/a | B6 + SSM parameters created | n/a |
| E1 End-to-end test in prod (full matrix) | n/a | D1 | n/a |
| C1 Legacy cleanup (**with Gus**) | both | E1 | n/a |

---

## B1: Models, states and validation (backend)

**Prompt:**

> Repo: `ecg-backend`, branch `tlk/card-58ed1492-bf5c-431f-b4ae-dfe6787488c9-5a5c322e99`. Read first:
> `docs/migration-plan-v1.md` (sections 0 and 0.1), `docs/user-flow-v1.md` (sections 2-4), the lambda contract
> (sections 2.1, 2.2, 3.1). Today the backend integrates a single lambda `ecg-analyser` (`analyze` + `refine`) and will
> move to the reading and interpretation lambdas of contract v1. This step only touches **models,
> migrations, input serializers and the removal of refine**: the lambda integration and the new views are
> steps B2/B3 (do not implement them).
>
> 1. **Remove refine** (decision D2): `AttemptRefineView` and the brush serializers in `exams/views.py`, the
>    `refine/` route in `exams/urls.py`, `run_refine` in `exams/services.py`, `exams/test_refine_contract.py`.
>    Remove `AWS_LAMBDA_ECG_ANALYSER` from `config/settings.py` and create `AWS_LAMBDA_ECG_READING`
>    (default `ecg-reading-mock`) and `AWS_LAMBDA_ECG_INTERPRETATION` (default `ecg-interpretation-mock`), plus
>    `ECG_TEST_SCENARIOS_ENABLED` (bool, default False).
> 2. **States** (`ExamStatus`): replace them with the ones from flow section 3: `created`, `reading`,
>    `awaiting_validation`, `reading_refused`, `reading_error`, `user_rejected`,
>    `interpreting`, `completed`, `interpretation_error`. A data migration maps the old ones
>    (on `Exam` and `ExamAttempt`): phase-A processing -> `reading_error`, awaiting approval -> `awaiting_validation`,
>    approved/completed -> `completed`, rejected -> `user_rejected`, phase-B processing -> `interpretation_error`,
>    error -> `reading_error`.
> 3. **`Patient`**: replace `gender` with `sex` (`CharField(max_length=1, choices F/M, null=True)`; null only for
>    legacy). Data migration: female spellings -> F, male spellings -> M, anything else -> NULL.
> 4. **`ExamAttempt`**: add `layout` (choices = the 6 layouts of contract 2.2), `reading` (JSONField null),
>    `interpretation` (JSONField null), `test_scenario` (JSONField null). Remove the old pipeline fields
>    (image type, mask and overlay keys, signals, lead labels, rhythm index, fidelity fields) and the
>    `FidelityVerdict` enum. Keep the image key and the calibration. Remove `Exam.ecg_layout` (the layout is now per attempt).
>    Format B2 will store in `reading`: `{status, warnings, error, meta, grid, leads:[{name,kind,quality,coverage}],
>    artifacts:{overlay:{bucket,key}, signal:{bucket,key}, refusal_preview:{bucket,key}|null}, refusal_region_px}`;
>    in `interpretation`: `{status, warnings, error, meta}`. Document this in a model docstring.
> 5. **`Report`**: replace the old fields with `classification` (normal/borderline/abnormal, null),
>    `summary` (JSON), `details` (JSON), `disclaimer` (Text), `warnings` (JSON, default list), `engine`,
>    `engine_version`. Keeps the `OneToOne` with the attempt.
> 6. **Input serializers** (`exams/serializers.py`): `ExamCreateSerializer` accepts `patient_id` **or**
>    `{name, age (0-130), sex F|M}` (exactly one of the two) + optional `reason`. `AttemptCreateSerializer`:
>    `filename`, `content_type` in {image/jpeg, image/png, image/webp} (matching extension), required `layout`
>    (6 values), `speed_mm_s` in {12.5, 25, 50} default 25, `gain_mm_mv` in {5, 10, 20} default 10,
>    optional `test_scenario` validated against the contract section 4 table (scenario `refused` is not accepted for
>    `interpret`); if present and (`ECG_TEST_SCENARIOS_ENABLED` is False or the user is not staff) -> 400 error
>    on the `test_scenario` field. Adjust the attempt creation view to store `layout` and `test_scenario` and
>    to return `upload_headers`. Output serializers can stay minimal/provisional (B3 redoes them), but the
>    project must start.
> 7. Update `exams/admin.py` and `tests/test_integration_standalone.py` (remove what tested the old pipeline).
>    Rewrite `exams/tests.py` for the new models/serializers (exam creation with sex, layout/calibration/content-type
>    validation, the `test_scenario` gate staff x non-staff x flag off, status and sex data
>    migrations). `exams/test_single_lambda_contract.py` tests the old contract: remove it.
>
> Acceptance criteria: `python manage.py makemigrations --check --dry-run` with nothing pending;
> `python manage.py test` green; `python -m unittest discover -s mock_lambdas/tests` still green;
> `grep -rn "ecg-analyser\|refine" exams config` only returns intentional hits (explain them).
>
> **Report to return:** list of changed/removed files; the content of the generated migrations (names and
> operations); how the `test_scenario` validation ended up; any point where this prompt was ambiguous and the
> decision you took; summarized test output (how many, which are new).

---

## B2: Lambda contract client (backend)

**Prompt:**

> Repo: `ecg-backend`, the card branch (see `docs/migration-plan-v1.md` §0). Read first: plan §0/§0.1,
> `docs/user-flow-v1.md` §3-4, the whole lambda contract, `mock_lambdas/ecg_contract/__init__.py`
> (code catalog). Step B1 already replaced the models: `ExamAttempt` has `layout`, `calibration`,
> `image_s3_key`, `reading` (JSON), `interpretation` (JSON), `test_scenario` (JSON); `Patient.sex` is F/M;
> `Report` has `classification`, `summary`, `details`, `disclaimer`, `warnings`, `engine`, `engine_version`;
> the states are the ones from flow section 3. Check the real code before starting.
>
> Goal: rewrite `exams/services.py` to speak contract v1. **No views and no async** (that is B3).
>
> 1. Keep `presign_put_url`/`presign_get_url`/`_s3_client` (SigV4). Remove everything from `ecg-analyser`
>    (the old `run_phase_a`, `run_phase_b`, `overlay_urls`).
> 2. `invoke_contract(function_name, payload) -> dict` that **always** returns a contract envelope:
>    - normal response: validate `status` in {OK, OK_WITH_WARNINGS, REFUSED, ERROR}, `warnings` a list, `error`
>      with `code`/`category`/`retryable`/`message` when the status is REFUSED/ERROR, `result` an object when OK*.
>      Malformed envelope -> synthetic retryable `ERROR` `INTERNAL_ERROR` envelope (log the truncated body).
>    - `FunctionError` (timeout/OOM/unhandled exception) or a boto3 exception -> synthetic `ERROR`
>      `INTERNAL_ERROR` envelope, `retryable: true`, generic message. Client read timeout =
>      `AWS_LAMBDA_READ_TIMEOUT`, `retries.max_attempts=0` (never re-invoke on its own).
> 3. `run_reading(attempt)`: builds the 2.1 payload (`request_id=str(attempt.id)`, `image={bucket: AWS_IMAGES_BUCKET,
>    key: attempt.image_s3_key}`, `layout`, `calibration`, `mock` = `attempt.test_scenario["read"]` only if it
>    exists), invokes `AWS_LAMBDA_ECG_READING` and applies the result:
>    - OK/OK_WITH_WARNINGS -> check that `artifacts.overlay` and `artifacts.signal` are in the
>      `AWS_RESULTS_BUCKET` bucket with the `reading/<attempt.id>/` prefix (otherwise treat it as `INTERNAL_ERROR`); store in
>      `attempt.reading` the summary (no `samples`!) and move to `awaiting_validation`.
>    - REFUSED -> store `error` + `details.preview` (same bucket/prefix check) + `details.region_px`;
>      state `reading_refused`.
>    - ERROR -> store `error`; state `reading_error`.
> 4. `run_interpretation(attempt)`: only from an OK* reading. Downloads `artifacts.signal` from S3 (JSON), builds the
>    3.1 payload with `patient.sex = attempt.exam.patient.sex` (if null -> local `ERROR` `SEX_MISSING`, without
>    invoking) and `mock = test_scenario["interpret"]` if present. OK* -> creates/updates `Report` and state
>    `completed`; ERROR -> `interpretation_error`. Stores `attempt.interpretation`. Failure downloading the signal ->
>    retryable `STORAGE_ERROR`.
> 5. API read functions: `attempt_reading_view(attempt)` and `attempt_interpretation_view(attempt)` that
>    return the `reading`/`interpretation` blocks of the §0.1 `Attempt` object, generating the presigned URLs on
>    the fly; and `attempt_flags(attempt) -> {can_retry, needs_new_photo}` following flow §3.
> 6. State changes: update `attempt.status` and `exam.status` together (the exam mirrors the attempt), in a
>    transaction.
> 7. Tests (`exams/test_services.py`), with the boto3 client mocked and JSON fixtures built from the
>    contract examples: the 4 reading statuses, the 3 interpretation ones, FunctionError, network exception,
>    malformed envelope, artifact outside the prefix, null sex, failure downloading the signal, `mock` only present
>    when there is a `test_scenario`, `samples` never stored in the database, M and F reaching the payload.
>
> Acceptance criteria: `python manage.py test` green; no reference to `ecg-analyser` left.
>
> **Report to return:** signatures of the public functions of `services.py`; the table "lambda status/code ->
> attempt state -> can_retry/needs_new_photo" as implemented; the exact bucket/prefix check
> snippets; decisions where the contract left room; list of the new tests.

---

## B3: Async tasks + views and routes (backend)

**Prompt:**

> Repo: `ecg-backend`, the card branch. Read first: plan §0/§0.1 (the API contract is the source of truth
> for this step), flow §3-4. Current state: B1 replaced models/states; B2 created in `exams/services.py`
> `run_reading(attempt)`, `run_interpretation(attempt)`, `attempt_reading_view`, `attempt_interpretation_view`,
> `attempt_flags` (check the real names in the code). Now: async execution, views and routes.
>
> 1. `exams/tasks.py`:
>    - `dispatch(task, attempt_id)` with `task` in {`read`, `interpret`}. `settings.ECG_TASK_MODE`:
>      `inline` (default; runs right away, used in dev and tests) or `sqs` (uses boto3 `sqs.send_message` on the
>      `settings.ECG_TASK_QUEUE_URL` queue, env `ECG_TASK_QUEUE_URL`, JSON body `{"ecg_task": task, "attempt_id": "<uuid>"}`;
>      the consumer is the B4 worker, which runs in another container). If the dispatch fails,
>      the attempt goes to `reading_error`/`interpretation_error` with a retryable `INTERNAL_ERROR`.
>    - `run_task(task, attempt_id)`: loads the attempt and only runs if it is in the matching processing
>      state (idempotent: a duplicate event does not run twice). Calls `run_reading`/`run_interpretation`.
>      Never lets an exception escape (logs and marks an error).
> 2. Views and routes exactly as in §0.1: `read/`, `GET attempt`, `accept/`, `reject/`, `retry/`, `GET /api/config/`,
>    `GET /api/health/` (no auth; `SELECT 1`; `version` comes from `settings.APP_VERSION`, env `APP_VERSION`, default
>    `dev`), and `is_staff` on login. Transitions with compare-and-swap (`filter(status=...).update(...)`), as the
>    current code already does, so two taps do not start two tasks. `retry` goes back to the processing state of the
>    failed phase and dispatches the task. `Exam`/`Attempt` output serializers in the §0.1 format. Remove the old
>    views and routes (process, approve, reject).
> 3. Stuck attempt recovery: adapt `_recover_stale_attempt`: an attempt in a processing state for longer than
>    `STALE_PROCESSING_SECONDS` becomes the phase error state with `{"code":"TASK_TIMEOUT","category":"internal","retryable":true,
>    "message":"Processing took longer than expected. Try again."}` stored in the phase block.
> 4. Isolation: every exam/attempt route filters by `doctor=request.user` (404 for another user).
> 5. API tests (`exams/test_api.py`, `inline` mode, lambdas mocked at the boto3 level or at
>    `services.invoke_contract`): happy path to `completed`; with warnings; refusal; retryable error + `retry`;
>    non-retryable error + `retry` -> 409; `reject` -> `user_rejected` and a new attempt on the same exam brings the
>    exam back to `created`; interpretation error + `retry`; double `read/` -> 409; another user's access -> 404;
>    `TASK_TIMEOUT`; `config` with and without staff/flag; `health` without a token; `dispatch` `sqs` mode calling
>    `send_message` on the right queue with the right body.
>
> Acceptance criteria: `python manage.py test` green; `python manage.py check` with no errors; a manual `curl` with
> `runserver` + SQLite going through create exam -> create attempt (the real PUT can be skipped) works.
>
> **Report to return:** table of implemented routes (method, route, status codes); a real JSON example of
> `GET attempt` in `awaiting_validation` and in `completed` (taken from a test); how `run_task` idempotency was
> guaranteed; deviations from §0.1 (if any, justify them); new tests.

---

## B4: Container, SQS worker, production settings and SPA (backend)

**Prompt:**

> Repo: `ecg-backend`, the card branch. Read first: plan §0 (target architecture: Django on **ECS Fargate ARM**,
> two containers from the same image, `web` with gunicorn and `worker` consuming SQS, RDS PostgreSQL database,
> SSM secrets injected as env vars by ECS). The backend already has the V1 API with async tasks
> (`exams/tasks.py`: `dispatch` / `run_task`, modes `ECG_TASK_MODE=inline|sqs`; check the real names).
> Now prepare the production image. **No real AWS in this step** (only code, tests and `docker build`).
>
> 1. Worker: command `exams/management/commands/ecg_worker.py`. Long polling loop on the
>    `ECG_TASK_QUEUE_URL` queue (`receive_message`, `WaitTimeSeconds=20`, `MaxNumberOfMessages=1`); for each
>    `{"ecg_task","attempt_id"}` message, call `run_task` and only then `delete_message` (`run_task` never
>    raises; invalid body -> log and delete). Handle `SIGTERM`/`SIGINT`: finish the message in progress and exit
>    with code 0 (ECS gives a 120 s `stopTimeout`). Close stale database connections between messages
>    (`django.db.close_old_connections()`). Log one line per message with `attempt_id`, task and duration.
> 2. Production settings (`config/settings.py`):
>    - Database: if `DATABASE_URL` exists, use it; otherwise build it from `DB_HOST`, `DB_PORT` (5432), `DB_NAME`,
>      `DB_USER`, `DB_PASSWORD` with `sslmode=require`. `conn_max_age=60`, `conn_health_checks=True`. If not
>      SQLite and `DEBUG=False`, require `sslmode` in (`require`, `verify-ca`, `verify-full`), otherwise
>      `ImproperlyConfigured`.
>    - `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` from env (they will hold the exact `*.execute-api.sa-east-1.amazonaws.com` host
>      and `localhost` for the container health check); `SECURE_PROXY_SSL_HEADER =
>      ("HTTP_X_FORWARDED_PROTO", "https")`, `SECURE_SSL_REDIRECT=False` (API Gateway is already HTTPS), `Secure`
>      cookies, `APP_VERSION` from env.
>    - WhiteNoise: middleware right after `SecurityMiddleware`, `STATIC_ROOT=BASE_DIR/"staticfiles"`, compressed
>      manifest storage.
>    - **SPA**: the Expo web export lives in `BASE_DIR/"frontend_dist"` (built at build time, outside git: add it
>      to `.gitignore`). Serve it with `WHITENOISE_ROOT=frontend_dist` and a catch-all view at the end of
>      `config/urls.py` that returns `frontend_dist/index.html` for any GET not starting with `api/`,
>      `admin/` or `static/` (if the file does not exist, 404 with a clear message).
>    - Logging: everything to stdout, one line per event, with `attempt_id` in the `tasks`/`services`/worker logs.
> 3. Command `accounts/management/commands/ensure_admin.py`: creates (if missing) a staff superuser with
>    `ADMIN_USERNAME`/`ADMIN_PASSWORD` from the environment; idempotent; never prints the password.
> 4. `Dockerfile` (replace the current one): base `public.ecr.aws/docker/library/python:3.12-slim` (ECR Public, avoids the
>    Docker Hub limit in CodeBuild), multi-stage if it reduces size, non-root user, `pip install
>    --no-cache-dir`, copies the code (without `tests/`, `mock_lambdas/`, `infra*/`, `docs/`; use `.dockerignore`),
>    copies `frontend_dist/` if it exists, runs `collectstatic --noinput` at build time (with a dummy `DJANGO_SECRET_KEY`
>    only at build time). `ENV PYTHONUNBUFFERED=1`. Default `CMD` = web:
>    `gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60
>    --access-logfile -` (the worker uses the same image with `command` overridden in the task definition).
>    `HEALTHCHECK` does not go in the Dockerfile (it lives in the task definition); but the image must be able to run
>    `python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/api/health/', timeout=3)"`.
> 5. `requirements.txt`: add `whitenoise`; remove `requests` if only `smoke_e2e.py` uses it (see C1).
> 6. Tests (SQLite, boto3 mocked with `botocore.stub.Stubber` or `unittest.mock`): the worker processes a valid
>    message (calls `run_task` and deletes), an invalid body is deleted without calling `run_task`, `SIGTERM` ends the
>    loop after the current message; settings build the database from `DB_*`; `GET /any/route` -> `index.html`
>    when `frontend_dist/index.html` exists (create a temporary one in the test) and `GET /api/missing/` is still
>    a JSON 404; `ensure_admin` is idempotent.
>
> Acceptance criteria: `python manage.py test` green; `DEBUG=False DJANGO_SECRET_KEY=x... ALLOWED_HOSTS=example.com
> DATABASE_URL=postgres://u:p@h/db?sslmode=require python manage.py check --deploy` with no errors (warnings
> explained); `docker build --platform linux/arm64 -t ecg-backend:local .` works (report the size); the
> image starts with `docker run` + SQLite and `/api/health/` answers 200.
>
> **Report to return:** image size; the list of env vars the task will need (separating SSM secrets
> from non-secrets); the `check --deploy` output; the worker snippet that guarantees "delete only after
> processing" and the `SIGTERM` handling; how the catch-all avoids swallowing the `/api/` 404 (paste the snippet);
> uncertain points.


---

## F1: API client and types in `packages/shared` (frontend)

**Prompt:**

> Repo: `ecg-reader` (branch `feat/expo-v1-flow`, create it from `main`). Read first, in the
> `ecg-backend` repo (card branch): `docs/migration-plan-v1.md` §0.1 (backend API contract: **source of
> truth**), `docs/user-flow-v1.md` §3-4, the lambda contract §3.2 (`summary`/`details` format).
> Also read `apps/mobile/AGENTS.md` and the current `createApiClient` in `packages/shared`.
>
> 1. In `packages/shared`, create the §0.1 TypeScript types (`AttemptStatus`, `Attempt`, `ReadingBlock`,
>    `InterpretationBlock`, `InterpretationSummary`, `InterpretationDetails`, `Exam`, `Patient`, `AppConfig`,
>    `Layout`, `TestScenario`, `ContractError`, `ContractWarning`).
> 2. Extend `createApiClient` (keeping `baseUrl` and `getToken`) with: `login`, `getConfig`, `listExams`,
>    `getExam`, `createExam`, `createAttempt`, `uploadImage(uploadUrl, headers, fileUri|Blob)` (direct PUT to
>    S3, same signed `Content-Type`; works on web and native), `startRead`, `getAttempt`, `accept`,
>    `reject`, `retry`, `sendFeedback`, and `pollAttempt(examId, attemptId, {intervalMs=2000, timeoutMs=300000,
>    signal})` that resolves when the status leaves the processing states.
> 3. Normalized errors: `ApiError {kind: "network"|"unauthorized"|"validation"|"conflict"|"server", status,
>    message (ready for the UI), fieldErrors?}`. 401 -> `unauthorized` (the app will log out).
> 4. Empty `baseUrl` = same origin (web served by Django); on native it comes from `EXPO_PUBLIC_API_BASE_URL`.
> 5. Remove from `shared` whatever only served the old flow (process, approve, reject, refine), if it exists,
>    without breaking `apps/web` more than needed (it will be retired; if its build breaks, say so).
> 6. Tests: if the repo has no runner, add `vitest` only in `packages/shared` and test `pollAttempt`
>    (mocked fetch, interval, timeout, abort) and the error normalization.
>
> Acceptance criteria: `npx tsc --noEmit` clean in `packages/shared` and `apps/mobile`; shared tests green.
>
> **Report to return:** exported signatures; how `uploadImage` handles web x native; what was
> removed; `tsc` and test results; mismatches found between §0.1 and what the backend
> actually returns (if you could check).

---

## F2: Screens: login -> exam -> photo -> layout -> processing (frontend)

**Prompt:**

> Repo: `ecg-reader`, branch `feat/expo-v1-flow`, app `apps/mobile` (Expo SDK 57: check
> `https://docs.expo.dev/versions/v57.0.0/` before using any Expo API). Read first, in `ecg-backend`:
> `docs/user-flow-v1.md` (sections 1, 3, 4) and `docs/migration-plan-v1.md` §0.1. `packages/shared` already has
> the API client and the types (step F1: `login`, `getConfig`, `createExam`, `createAttempt`, `uploadImage`,
> `startRead`, `pollAttempt`...; check the real names). Today the app only has `LoginScreen` and `UploadScreen`, with
> problems. Reuse the `shared` components (`Button`, `Input`, `ChipSelect`, `Header`...) where they fit.
>
> 1. Navigation (`src/navigation/RootNavigator.tsx`): auth stack x app stack, decided by the stored token;
>    a 401 on any call -> clear the token and go back to login.
> 2. Screens: `LoginScreen` (fix it; readable error; loading), `ExamsScreen` (exam list with status and
>    "New exam"; pull-to-refresh), `NewExamScreen` (name, age, sex F/M required in a segmented control;
>    inline validation), `CaptureScreen` (camera or gallery through `expo-image-picker`, preview, speed and gain
>    with the `getConfig` values, default 25/10), `LayoutScreen` (6 layouts with a mini diagram of each
>    grid, per table 2.2 of the contract), `ProcessingScreen` (creates the attempt, uploads, calls
>    `startRead`, polls with a progress message and a cancel button; when done, navigates according to the state;
>    the destination screens are F3, for now use a placeholder that shows the state JSON).
> 3. **Test scenario** selector (D7): only shows on `CaptureScreen` when `config.test_scenarios_enabled`
>    is `true`. Lets you pick the reading and interpretation scenario + code (values from contract section 4);
>    it goes into `createAttempt` as `test_scenario`. Visually marked as "Test mode".
> 4. Accessibility and UX: `accessibilityLabel` on controls, touch targets >= 44 pt, scalable text,
>    loading/error/empty states on every screen, no broken layout on web (max width ~480 px, centered) or on
>    small screens.
> 5. Configuration: `EXPO_PUBLIC_API_BASE_URL` in `.env.example`; on web, if empty, use the same origin.
>
> Acceptance criteria: `npx tsc --noEmit` clean; `npx expo start --web` opens and goes through login -> exams -> new
> exam -> photo -> layout -> processing against a local backend (`ecg-backend`: `python manage.py runserver` with
> `ECG_TASK_MODE=inline` and the real AWS mock lambdas, if there are credentials; otherwise against a backend with
> `services.invoke_contract` stubbed; say which one you used). Screenshots of the screens in the report.
>
> **Report to return:** list of screens and navigation routes; screenshots (web) of each screen; how
> polling is cancelled when leaving the screen; backend problems found (with the JSON received) for me to check;
> UI decisions taken without instructions.

---

## F3: Screens: visual validation, refusal, error, result and history (frontend)

**Prompt:**

> Repo: `ecg-reader`, branch `feat/expo-v1-flow`, `apps/mobile` (Expo SDK 57; check the versioned docs).
> Read first, in `ecg-backend`: `docs/user-flow-v1.md` §3 and **§4 (outcome table: source of truth
> for this step)**, `docs/migration-plan-v1.md` §0.1 and the lambda contract §2.3, §2.4, §3.2. F2 already delivered
> navigation, login, exams, new exam, capture, layout and `ProcessingScreen` (which navigates to a placeholder);
> check the real code. Now the outcome screens:
>
> 1. `ValidationScreen` (`awaiting_validation`): image `reading.overlay_url` (gridlines + trace) with zoom of
>    at least 3x and panning on all three platforms (iOS, Android, web); warnings (`reading.warnings`) highlighted
>    above the buttons; per-lead quality (`reading.leads`, chips good/partial/saturated/low); buttons
>    **Accept reading** (-> `accept` -> interpretation `ProcessingScreen`) and **Reject** (confirmation -> `reject`
>    -> offers a new photo on the same exam).
> 2. `RefusedScreen` (`reading_refused`): `error.message`, preview `refusal_preview_url` (highlight
>    `refusal_region_px` if there is no preview), tips per code (`READ_REFUSED_BLURRY` -> "rest the phone...",
>    etc., for the 6 codes), **Take a new photo** button.
> 3. `ErrorScreen` (`reading_error` / `interpretation_error` and network failures): specific message; **Try
>    again** (`retry`) if `can_retry`, otherwise **Take a new photo** (`needs_new_photo`). Network error -> re-fetch
>    the attempt (the state is not lost). **Outside business hours** (the backend is off Mon-Fri 20:00-08:00 and on
>    weekends; API Gateway returns 503 without a Django JSON body): any 503 whose body is not backend JSON
>    -> notice "Service available Monday to Friday, 8 am to 8 pm" (text in a single constant), with no
>    automatic retry loop. Includes the login screen.
> 4. `ResultScreen` (`completed`): highlighted summary (color/icon per `classification`, `headline`, rhythm, HR,
>    `key_findings`, `text`); interpretation and reading warnings at the top; expandable details: measurements
>    (table with value, unit and confidence; `null` = "not measurable"), findings (with severity), criteria (badge
>    "sex-dependent" when `sex_specific`; status normal/abnormal/not_evaluated), per lead; `disclaimer`
>    always visible; "Send feedback" button.
> 5. `ExamDetailScreen`: the exam's attempt history with status; tapping an attempt opens the screen for its
>    state (reopening the app mid-flow must lead to the right screen).
> 6. Color can never be the only indicator (text/icon alongside). All texts in English.
>
> Acceptance criteria: `npx tsc --noEmit` clean; against the local backend with test scenarios on
> (`ECG_TEST_SCENARIOS_ENABLED=true`, staff user), go through on web: reading `success`,
> `success_with_warnings`, `refused` (at least `READ_REFUSED_BLURRY` and `READ_REFUSED_OCCLUDED`), `failure` with
> `INTERNAL_ERROR` ("Try again" must show) and with `READ_TRACE_EXTRACTION_FAILED` ("Take a new photo"
> must show), rejection by the user, interpretation `success` with F and with M (the QTc reference changes),
> `success_with_warnings` and `failure`.
>
> **Report to return:** a screenshot of each case above; how zoom was implemented on each platform;
> list of scenarios you could not reproduce and why; inconsistencies of the backend JSON against §0.1.

---

## F4: Web export served by Django + local end-to-end check

**Prompt:**

> Repos: `ecg-reader` (branch `feat/expo-v1-flow`) and `ecg-backend` (card branch). Read first:
> `docs/migration-plan-v1.md` §0 (architecture: the Expo web export is served by Django itself (WhiteNoise), same
> origin as the API). The backend already serves `frontend_dist/` through WhiteNoise + a catch-all to `index.html` (step B4)
> and the app has the full flow (F2/F3).
>
> 1. `apps/mobile/app.json`: `web.output = "single"` (SPA). Make sure routes/links work from `/`
>    and that assets come out with absolute `/...` paths compatible with WhiteNoise.
> 2. Script `ecg-backend/scripts/build_frontend.sh <path-to-ecg-reader>`: `npm ci` at the monorepo root,
>    `npx expo export --platform web` in `apps/mobile` with `EXPO_PUBLIC_API_BASE_URL=` (empty = same origin),
>    copies `dist/` to `ecg-backend/frontend_dist/`.
> 3. Local check: `build_frontend.sh` + `python manage.py collectstatic` + `runserver` (or the B4 Docker
>    image with `docker run`) and go through on web, **served by Django**, the same cases as F3. Check that
>    a missing `/api/...` still returns a JSON 404 and that reloading the page on an inner app route
>    returns the app.
> 4. Run the app on **Expo Go** (phone on the same network, `EXPO_PUBLIC_API_BASE_URL=http://<machine-ip>:8000`,
>    `ALLOWED_HOSTS` including the IP) and go through at least: full success, refusal and retryable error.
> 5. Update the backend `README.md` (local run and build sections): short.
>
> Acceptance criteria: F3 cases passing on the web served by Django; the 3 cases on Expo Go.
>
> **Report to return:** `frontend_dist` size; screenshots (web served by Django + Expo Go);
> asset path/routing problems found and the fix; anything that works in
> `expo start --web` but not in the export.

---

## I1: Terraform: network, RDS, ECR, SQS and IAM (`plan` only)

**Prompt:**

> Repo: `ecg-backend`, the card branch. Read first: `docs/migration-plan-v1.md` §0 (target architecture, list of what
> is forbidden) and §0.2 (cost), and the env vars the backend needs (`config/settings.py`, `Dockerfile`,
> `exams/management/commands/ecg_worker.py`, `README.md`). Style reference: `mock_lambdas/infra/`.
> **Do not run `terraform apply`.** Before using write-only arguments, check in the provider docs that
> the chosen version supports them.
>
> Create `infra/aws/` (Terraform >= 1.11, aws provider at the latest stable version that supports `password_wo`
> on `aws_db_instance` and the ephemeral `aws_ssm_parameter`), S3 backend `ecg-lambdas-terraform-state`, key
> `ecg-backend-prod/terraform.tfstate`, region `sa-east-1`, `default_tags` `project=ecg`, `env=prod`.
> Files split by topic (`network.tf`, `rds.tf`, `ecr.tf`, `sqs.tf`, `iam.tf`, `variables.tf`, `outputs.tf`).
> 1. Network: VPC `10.20.0.0/16` with DNS enabled; 2 public subnets (2 AZs) with an IGW; 2 private subnets (2 AZs)
>    with a route table **without** an internet route. No NAT, no VPC endpoints. Security groups:
>    `ecg-vpclink` (no ingress; egress 8000 to `ecg-ecs`), `ecg-ecs` (ingress 8000 only from `ecg-vpclink`;
>    egress 443 to 0.0.0.0/0 and 5432 to `ecg-rds`), `ecg-rds` (ingress 5432 only from `ecg-ecs`).
> 2. RDS PostgreSQL (latest available 17.x): `db.t4g.micro`, Single-AZ, gp3 20 GB with
>    `max_allocated_storage = 50`, encrypted (AWS managed key), subnet group on the private subnets,
>    `publicly_accessible = false`, `backup_retention_period = 7`; a 03:00-04:00 UTC backup window does **not**
>    work (the database is off overnight): use a window within the on-hours, e.g. `14:00-14:30` UTC, and
>    maintenance `wed:15:00-wed:15:30` UTC; `deletion_protection = true`, `skip_final_snapshot = false`,
>    `copy_tags_to_snapshot = true`, `auto_minor_version_upgrade = true`, Performance Insights/Database Insights
>    on the free tier (7 days), parameter group with `rds.force_ssl = 1` and `log_min_duration_statement = 1000`.
>    Master user `ecg_admin`; password **outside the state**: read from SSM `/ecg/prod/DB_PASSWORD` (created by hand by
>    Gus) with `ephemeral "aws_ssm_parameter"` and passed in `password_wo` + `password_wo_version` (variable).
>    Initial database `ecg`.
> 3. ECR `ecg-backend`: `image_tag_mutability = IMMUTABLE`, scan on push, lifecycle keeping the 10 most
>    recent images.
> 4. SQS `ecg-tasks` (SSE-SQS, `visibility_timeout_seconds = 900`, `receive_wait_time_seconds = 20`,
>    `message_retention_seconds = 86400`) with DLQ `ecg-tasks-dlq` (`maxReceiveCount = 3`, 14-day retention).
> 5. IAM (least privilege, roles only; I2 uses them):
>    - `ecg-ecs-execution`: `AmazonECSTaskExecutionRolePolicy` + `ssm:GetParameters` on
>      `arn:...:parameter/ecg/prod/*` + `kms:Decrypt` on `alias/aws/ssm` with `kms:ViaService =
>      ssm.sa-east-1.amazonaws.com`.
>    - `ecg-ecs-task`: `s3:PutObject` on the images bucket `ecg-lambdas-images-785236377332-sa-east-1`
>      (prefix `*/original.*`, to sign the PUT); `s3:GetObject` on that bucket and on
>      `ecg-lambdas-results-785236377332-sa-east-1`; `lambda:InvokeFunction` only on the two contract lambdas
>      (names by variable, default `ecg-reading-mock` / `ecg-interpretation-mock`); `sqs:SendMessage`,
>      `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes`, `ChangeMessageVisibility` only on `ecg-tasks`.
> 6. Outputs: `vpc_id`, public/private subnet ids, the 3 SG ids, `db_endpoint`, `db_name`, `db_username`,
>    `ecr_repository_url`, `queue_url`, `queue_arn`, `dlq_arn`, ARNs of the 2 roles.
> 7. Short `infra/aws/README.md`: prerequisites, SSM parameters to create by hand (names and `SecureString` type, no
>    values: `DB_PASSWORD`, `DJANGO_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`), `init/plan/apply` commands.
>
> Acceptance criteria: `terraform fmt -check`, `terraform validate` and `terraform plan` with no errors (the plan needs
> the `/ecg/prod/DB_PASSWORD` parameter to exist; if it does not, run the plan with `-target` excluding RDS and say
> so in the report). Nothing applied.
>
> **Report to return:** `plan` resources (count by type); the policies of the 2 roles (JSON); proof that
> the database password does not appear in the state/plan (plan snippet showing `password_wo` hidden); the
> PostgreSQL and provider versions chosen; resources that cost money while idle (expected: only the RDS disk and ECR);
> uncertain points.

---

## I2: Terraform: ECS Fargate, API Gateway, business hours and monitoring (`plan` only)

**Prompt:**

> Repo: `ecg-backend`, the card branch. Read first: `docs/migration-plan-v1.md` §0 and §0.2, and `infra/aws/` (step
> I1: VPC, subnets, SGs `ecg-vpclink`/`ecg-ecs`/`ecg-rds`, RDS, ECR, SQS, roles `ecg-ecs-execution`/`ecg-ecs-task`;
> **check the real resource and output names in the code**). Also the `Dockerfile` and the
> `ecg_worker` command. **Do not run `terraform apply`.** Same state and folder as I1, new files (`ecs.tf`, `api.tf`,
> `schedule.tf`, `monitoring.tf`).
>
> 1. ECS: cluster `ecg` (Container Insights **off**). Task definition `ecg-backend`: Fargate, `ARM64`,
>    `cpu = var.task_cpu` (default 512), `memory = var.task_memory` (default 1024), `awsvpc`, I1 roles, image
>    `<ecr>:${var.image_tag}` (default `bootstrap`). Two containers:
>    - `web` (essential, port 8000, health check with the B4 `python -c urllib.../api/health/`, interval 30,
>      startPeriod 60);
>    - `worker` (essential, `command = ["python","manage.py","ecg_worker"]`, `stopTimeout = 120`).
>    Both with the `awslogs` log driver on the `/ecs/ecg-backend` log group (14 days), non-secret env vars
>    (`DEBUG=False`, `ECG_TASK_MODE=sqs`, `ECG_TASK_QUEUE_URL`, `DB_HOST`, `DB_NAME`, `DB_USER`,
>    `AWS_IMAGES_BUCKET`, `AWS_RESULTS_BUCKET`, `AWS_LAMBDA_ECG_READING`, `AWS_LAMBDA_ECG_INTERPRETATION`,
>    `ECG_TEST_SCENARIOS_ENABLED` = variable defaulting to `true`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `APP_VERSION`)
>    and `secrets` from SSM (`DB_PASSWORD`, `DJANGO_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`).
> 2. Service `ecg-backend`: Fargate (`platform_version = "LATEST"`), **public** subnets with
>    `assign_public_ip = true`, SG `ecg-ecs`, `deployment_circuit_breaker { enable = true, rollback = true }`,
>    min healthy 100 % / max 200 %, `enable_execute_command = false`, `service_registries` pointing to a
>    Cloud Map service (private DNS namespace `ecg.internal` in the VPC, **SRV** record, `container_name = "web"`,
>    `container_port = 8000`). `lifecycle { ignore_changes = [desired_count, task_definition] }` (the pipeline does
>    the deploy and the Scheduler/autoscaling controls the count).
> 3. Autoscaling (Application Auto Scaling): target `min 1 / max 2`, CPU target tracking 70 %. **Scheduled actions**
>    in the schedule timezone: `cron(0 8 ? * MON-FRI *)` -> min 1/max 2; `cron(0 20 ? * MON-FRI *)` -> min 0/max 0.
>    Times in variables.
> 4. RDS on/off: EventBridge Scheduler (group `ecg`, schedule timezone) with universal targets
>    `arn:aws:scheduler:::aws-sdk:rds:startDBInstance` (`cron(40 7 ? * MON-FRI *)`) and `...:stopDBInstance`
>    (`cron(10 20 ? * MON-FRI *)`), its own role with only `rds:StartDBInstance`/`StopDBInstance` on the instance.
>    Variable `office_hours_enabled` (default `true`): if `false`, no scheduled action is created and the minimum stays 1.
> 5. API Gateway **HTTP API**: VPC Link (public subnets, SG `ecg-vpclink`); private `HTTP_PROXY` integration
>    with `integration_uri` = the Cloud Map service ARN, `connection_type = VPC_LINK`, timeout 30 s; route
>    `$default`; stage `$default` with auto-deploy, throttling (burst 50, rate 20) and a JSON access log in its own log
>    group (14 days).
> 6. Scripts `scripts/start.sh` and `scripts/stop.sh` (`set -euo pipefail`, using the AWS CLI): `start` starts
>    RDS (if stopped), waits for `db-instance-available`, sets the autoscaling target to min 1/max 2 and waits for the
>    service to stabilize; `stop` does the reverse. For use outside business hours.
> 7. Monitoring: SNS topic `ecg-alerts` with an email subscription (variable `alarm_email`). Exactly 10
>    alarms (free tier), all with `treat_missing_data = notBreaching` and an SNS action: API Gateway 5xx >= 5 in
>    5 min; ECS service CPU > 80 % for 10 min; ECS service memory > 85 % for 10 min; RDS CPU > 80 % for
>    10 min; `FreeStorageSpace` < 2 GB; `CPUCreditBalance` < 20; DLQ `ApproximateNumberOfMessagesVisible` >= 1;
>    queue `ApproximateAgeOfOldestMessage` > 300 s; `Errors` of each contract lambda >= 1 in 5 min.
> 8. Cost: monthly `aws_budgets_budget` of US$35 with an email notice at 80 % actual and 100 % forecast.
> 9. Outputs: `api_url`, `cluster_name`, `service_name`, `task_family`, `log_group`, `sns_topic_arn`.
> 10. Images bucket CORS: **do not manage it here** (it belongs to the `ecg-lambdas` repo,
>     `infra/terraform/modules/shared`). Write the exact change in the report (add the origin
>     `https://<api-id>.execute-api.sa-east-1.amazonaws.com` to the PUT `AllowedOrigins`).
> 11. Update `infra/aws/README.md`: business hours, `start.sh`/`stop.sh`, how to change
>     `task_cpu`.
>
> Acceptance criteria: `terraform fmt -check`, `validate` and `plan` (with `-var alarm_email=...`) with no errors;
> scripts pass `shellcheck` (if available). Nothing applied.
>
> **Report to return:** new `plan` resources; the rendered task definition (JSON, without secrets); the
> 10 alarms (name, metric, threshold); the scheduled actions with timezone; the CORS change to make in `ecg-lambdas`;
> uncertain points, especially: the `Host` that reaches Django through the VPC Link (`ALLOWED_HOSTS` depends on it) and whether
> the SRV record + container health work with the HTTP API Cloud Map integration.

---

## I3: CI/CD on AWS: CodePipeline V2 + CodeBuild (`plan` only)

**Prompt:**

> Repo: `ecg-backend`, the card branch. Read first: `docs/migration-plan-v1.md` §0, `infra/aws/` (I1 and I2: ECR
> `ecg-backend`, cluster `ecg`, service `ecg-backend`, task definition `ecg-backend` with containers `web` and
> `worker`, SNS `ecg-alerts`, script `start.sh`; **check the real names**), `Dockerfile` and
> `scripts/build_frontend.sh`. **Do not run `terraform apply`.**
>
> 1. Buildspecs in `ci/`:
>    - `buildspec-test.yml` (Python 3.12): installs `requirements.txt`, runs `python manage.py makemigrations
>      --check --dry-run`, `python manage.py test`, `python -m unittest discover -s mock_lambdas/tests`.
>    - `buildspec-build.yml` (privileged, Node 22 + Docker): secondary source `ecg-reader` in
>      `$CODEBUILD_SRC_DIR_frontend`; `scripts/build_frontend.sh "$CODEBUILD_SRC_DIR_frontend"`; `APP_VERSION` =
>      `<short backend sha>-<short front sha>`; `docker build --build-arg APP_VERSION` and push to ECR with the
>      `APP_VERSION` tag (immutable tags: if the tag already exists, skip the push). Output artifact: `image.env`
>      (`IMAGE_TAG=...`) + `ci/deploy.sh`.
>    - `buildspec-deploy.yml` calling `ci/deploy.sh`.
> 2. `ci/deploy.sh` (`set -euo pipefail`):
>    1. If RDS is stopped or the autoscaling target is at max 0 (outside business hours), run
>       `scripts/start.sh` and note in the log that the environment stays on until the next scheduled shutdown.
>    2. Read the service's current task definition, switch the `web` and `worker` container images to the new tag and
>       `APP_VERSION`, register a new revision.
>    3. Migration: `aws ecs run-task` with the new revision, same subnets/SG/public IP as the service, `command`
>       overridden on the `web` container to `python manage.py migrate --noinput` and on `worker` to
>       `python -c "pass"`; wait for `tasks-stopped` and fail if the `web` exit code is not 0 (print the log tail).
>    4. `ensure_admin` the same way (idempotent).
>    5. `aws ecs update-service` with the new revision and `aws ecs wait services-stable` (the circuit breaker rolls back
>       on its own if the tasks do not become healthy).
>    6. Smoke: `curl` on `<api_url>/api/health/` (expects 200 and `version` equal to the new one) and on `<api_url>/`
>       (expects HTML). On failure, `update-service` back to the previous revision, wait for it to stabilize and exit with an error.
> 3. Terraform in `infra/aws/cicd.tf` (same state):
>    - GitHub `aws_codestarconnections_connection` (stays PENDING until Gus authorizes it in the console; document it).
>    - 3 CodeBuild projects (`ecg-backend-test`, `-build`, `-deploy`): ARM
>      (`aws/codebuild/amazonlinux-aarch64-standard:3.0`, `BUILD_GENERAL1_SMALL`; `build` with
>      `privileged_mode = true`), logs with 14-day retention, 20 min timeout, separate minimal roles (the
>      `build` only pushes to the `ecg-backend` ECR; `deploy` only touches the ECS service/task definition, `run-task`
>      with `iam:PassRole` limited to the 2 task roles, `rds:StartDBInstance`/`DescribeDBInstances` on the instance,
>      `application-autoscaling` on the service target, read access to the migration logs).
>    - Pipeline artifact bucket `ecg-backend-pipeline-<account_id>` (public access block, SSE-S3, lifecycle
>      deleting objects after 30 days).
>    - CodePipeline **V2**, `QUEUED` mode. Stages: Source (two actions: `tl-gmiler/ecg-backend` and
>      `trustedlake/ecg-reader` (own connection, `ecg-github-frontend`), branch `main`, artifacts `backend` and `frontend`; trigger on a push to `main` of
>      either) -> Test -> Build -> Deploy.
>    - Pipeline failure notification on SNS `ecg-alerts` (EventBridge rule).
> 4. Remove `.github/workflows/docker-publish.yml` (CI/CD moves to AWS); mention it in the report.
> 5. Update `infra/aws/README.md`: authorizing the GitHub connection, triggering the pipeline by hand, what
>    happens on a deploy outside business hours.
>
> Acceptance criteria: `terraform fmt -check`, `validate`, `plan` with no errors; `ci/deploy.sh` passes `shellcheck`
> (if available).
>
> **Report to return:** `plan` resources; the policies of the 3 CodeBuild roles and the pipeline role (JSON); the
> full `ci/deploy.sh`; estimated minutes per run (free tier: 100 CodeBuild min/month and 100 CodePipeline V2
> action min) and cost; uncertain points (e.g. the secondary source variable name, the ARM
> `docker build` time).

---


## B6: Reading on contract 2.0 (`ecg-reading`), done on 2026-10-19

Reason: between 2026-10-15 and 2026-10-19, backend PRs #6, #7 and #8 and PR #2 of `ecg-lambdas-v1` moved the mocks and the
contract to `ecg-lambdas-v1`, connected the old backend to the real `ecg-reading` (contract 2.0) and deleted the
`-mock` lambdas. What changed in V1:

- Merge of `origin/main`, keeping the V1 rewrite and porting Chris's logic.
- `exams/services.py`:
  - keeps the `OK` method with the highest `selector_score`;
  - overlay = the method's `files.image` and the interpretation `signal` = `files.leads` (`leads.json`), with
    the method's `sampling_rate_hz` and `units` mV;
  - warnings come from the chosen method; `METHODS_PARTIAL_RESULT` is added when the lambda says `OK_WITH_WARNINGS`
    without any warning;
  - on refusal, the preview is the first method's refusal image;
  - `reading.method` / `reading.methods` keep the choice (audit, outside the API);
  - an unreadable `leads.json` becomes a retryable `STORAGE_ERROR`.
- Accepted layouts: only `3x4+1` and `12x1` (the others return `LAYOUT_UNSUPPORTED` on 2.0). The reading test
  scenario no longer takes a `code`.
- Timings: boto `read_timeout` 930 s, `STALE_PROCESSING_SECONDS` 1200 s, `ecg-tasks` visibility 960 s,
  queue age alarm 600 s, app polling up to 20 min. The worker runs `ECG_WORKER_CONCURRENCY` (4)
  threads, so a 50-190 s reading does not hold up the queue. A reading cut off by the `stopTimeout`
  (120 s) during a deploy returns to the queue after the visibility timeout and is redone.
- Local backend: `scripts/local_server.py`, with the fake 2.0 reading in `scripts/local_lambdas.py` and the
  mock interpretation from the `ecg-lambdas-v1` clone (`--lambdas-repo`).
- To confirm in E1, against the real lambda: the code and format of the refusal and error for the `mock`
  scenarios (the local fake uses `READ_REFUSED_UNREADABLE_TRACE` / `READ_TRACE_EXTRACTION_FAILED`), where the
  `success_with_warnings` warnings come from, and the cold start time of the 3.5 GB image.

E1 below was written for contract v1: the per-code `refused`/`failure` cases become one case
per scenario, and a real reading case (without `mock`) with a real ECG photo is added.


## D1: First prod deploy (run **together with Gus**, each step confirmed)

Checklist (not a prompt for an agent to run alone):

1. Gus: create the `SecureString` SSM parameters in `sa-east-1`: `/ecg/prod/DB_PASSWORD` (generate with
   `python -c "import secrets;print(secrets.token_urlsafe(32))"`), `/ecg/prod/DJANGO_SECRET_KEY` (same, 50),
   `/ecg/prod/ADMIN_USERNAME`, `/ecg/prod/ADMIN_PASSWORD`.
2. `terraform -chdir=infra/aws apply` (review the plan first; RDS takes ~10 min) -> note `api_url`. The service
   starts with the `bootstrap` image, which does not exist yet: it keeps trying until the first pipeline deploy
   (expected; the circuit breaker does not act before the first deploy).
3. Gus: authorize both GitHub connections in the console (CodePipeline -> Settings -> Connections): `ecg-github`
   on the `tl-gmiler` account (backend) and `ecg-github-frontend` on the `trustedlake` account (the frontend repo was transferred
   there); and confirm the SNS email subscription.
4. Apply the images bucket CORS change in the `ecg-lambdas` repo (I2 report): only for web
   uploads; native Expo Go does not need CORS.
5. Merge the branches into `main` of both repos (reviewed PRs) -> the pipeline runs on its own (migrate + `ensure_admin`).
   Follow it; check in the task log which `Host` arrives and adjust `ALLOWED_HOSTS` if needed.
6. Sign in to `/admin/` with the admin and create the staff test user.
7. Check the schedule: the next day, see in the console that RDS stopped at 20:10 and the service went to 0 at 20:00, and that
   they came back at 07:40/08:00.
8. Expo Go: `EXPO_PUBLIC_API_BASE_URL=<api_url> npx expo start` (or EAS Update, if we decide later).


## E1: End-to-end test in prod

**Prompt:**

> Repos `ecg-backend` (card branch) and `ecg-reader`. The system is in prod: API and web at `<api_url>`
> (provided by Gus), mock lambdas with selectable scenarios, a staff test user (credentials provided
> by Gus outside the repo). Read `docs/user-flow-v1.md` §4 and the lambda contract §4-5.
>
> 1. Script `scripts/e2e_prod.py` (replaces `smoke_e2e.py`): through the API, with a valid test photo (>= 800x500 px)
>    in `tests/fixtures/`, goes through the matrix below, checking the final state, `can_retry`/`needs_new_photo`,
>    presence of the URLs and that the presigned URLs answer 200. Output as a table + non-zero exit code if anything
>    fails. Credentials from env (`E2E_USERNAME`, `E2E_PASSWORD`, `E2E_API_URL`).
> 2. Minimum matrix:
>    - reading `success` -> accept -> interpretation `success` (sex F and sex M; check the QTc reference)
>    - reading `success_with_warnings` -> accept -> interpretation `success_with_warnings`
>    - reading `refused` with each of the 6 `READ_REFUSED_*` codes
>    - reading `failure` with `READ_TRACE_EXTRACTION_FAILED`, `READ_GRID_NOT_FOUND` (needs_new_photo) and
>      `STORAGE_ERROR`, `INTERNAL_ERROR` (can_retry -> `retry` -> without a failure scenario on the retry it must complete)
>    - reading `success` -> reject -> new attempt on the same exam
>    - interpretation `failure` with `INTERP_NO_QRS_DETECTED`, `INTERP_SIGNAL_TOO_NOISY`, `INTERNAL_ERROR`
>    - real validation: image too small (`IMAGE_TOO_SMALL`) and a file that is not an image
>      (`IMAGE_FORMAT_UNSUPPORTED`/`IMAGE_UNREADABLE`)
>    - security: another user's exam route -> 404; no token -> 401; `test_scenario` with a non-staff user -> 400
> 3. Manual UI script (web in the browser **and** Expo Go Android/iOS) covering at least one case from each row
>    of flow table §4; save screenshots in `docs/evidence/e2e-prod/` and the results table in
>    `docs/evidence/e2e-prod/RESULTS.md`.
> 4. Observability: check that the attempt logs show up in CloudWatch with `attempt_id` and that an alarm
>    fires (e.g. forcing `INTERNAL_ERROR` is not enough; use `aws cloudwatch set-alarm-state` to test SNS).
>
> **Report to return:** the results table (case, expected, actual, evidence); bugs found with
> steps to reproduce and the response JSON; average reading and interpretation times (including the first request of the day, right after the service starts).

## C1: Legacy cleanup (with Gus's confirmation for each item)

- Disable and then delete the ECG CloudFront distributions: `E1919M81GCZ1D7` (the ecg-backend HTTPS
  proxy), `E35D6VSM7GCHQ8` (the legacy React SPA), `E2DDW6M1G1CAEK` (the dev SPA). **Ask**
  about `E1ZP3N2CBOTW7S` (the marketing site): it is not part of the app.
- Shut down the Oracle VM and remove `infra/terraform/` (EC2 + CloudFront), `infra-oracle/`, `infra/deploy.sh`,
  `smoke_e2e.py`, `ECG_INTEGRATION.md` (the useful content goes to the README).
- Old database (Neon): **no data migration** (decided on 2026-10-15); the prod RDS starts clean; Neon is simply abandoned.
- Retire `ecg-reader/apps/web` and the legacy SPA buckets (confirm before deleting data).
- Record in the KB (tags `ecg`, `aws`, `deploy`, `architecture`) the final architecture and the link to `infra/aws/README.md`:
  input for the diagram, deploy instructions and technical report cards.
