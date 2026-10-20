# User flow: V1 (revision of 2026-10-15)

> Status: **APPROVED for implementation.** D1-D4 and D8 decided with Gus on 2026-10-15 (D3/D4/D6 revised the
> same day: everything on AWS, Django on ECS Fargate, RDS database). The execution plan is in [`migration-plan-v1.md`](migration-plan-v1.md).

## 1. Target flow (planning, KB "ECG: Planning")

1. Login
2. New exam: patient data (name, age, **sex F/M**, used by the interpretation)
3. Upload/photo of the paper ECG (JPG/PNG/WebP) + calibration (speed and gain)
4. Lead layout choice (`3x4`, `3x4+1`, `3x4+3`, `6x2`, `6x2+1`, `12x1`)
5. **Reading** (reading lambda): signal + original image with the extracted trace and gridlines
   - Lambda refusal (blurred/cropped/overlapping image...) -> show the reason + preview and ask for a new photo
6. Visual validation by the user -> **accept** or **reject**
   - Reject -> new attempt (new photo) on the same exam
7. **Interpretation** (interpretation lambda): signal + sex -> overall summary + detailed result
8. Result screen: highlighted summary + expandable details + warnings + a notice that it is not a medical report

## 2. Decisions

| # | Topic | Decision | Status |
|---|---|---|---|
| D1 | Contract | ~~Contract v1 with the mocks `ecg-reading-mock` / `ecg-interpretation-mock`.~~ **Revised on 2026-10-19 (B6):** real reading `ecg-reading` on contract 2.0 and mock interpretation `ecg-interpretation` (repo tl-gmiler/ecg-lambdas-v1). Test scenarios are the payload `mock` field on the same functions; the `-mock` lambdas were deleted. | **decided** |
| D2 | Refine (brush) | Out of V1. Rejecting the reading = new photo. The `refine` code and the `ecg-analyser` integration are removed from the backend. | **decided** |
| D3 | Hosting | **Everything on AWS.** Django on **ECS Fargate ARM** (0.5 vCPU / 1 GB, 1-2 tasks) behind API Gateway HTTP API + VPC Link + Cloud Map (native `execute-api` HTTPS, no ALB). Web app (Expo export) served by Django itself through WhiteNoise. Tasks in a public subnet with a closed SG (no NAT). **No CloudFront.** Lambda for Django was ruled out (preference for container best practices). | **decided** |
| D4 | Database | **RDS PostgreSQL `db.t4g.micro` Single-AZ**, gp3 20 GB, private subnet, TLS required, 7-day backups, deletion protection. No Multi-AZ. Password in SSM (outside the Terraform state). | **decided** |
| D5 | Account/region | Account `785236377332`, region `sa-east-1`. | assumed |
| D6 | Processing | Asynchronous: the endpoint starts the task and answers 202, the task goes to an SQS queue (with a DLQ) consumed by a `worker` container in the same ECS task, which calls the reading/interpretation lambda, and the app polls the state (every 2 s). Reason: API Gateway cuts off at 30 s, and the real scripts can take longer than the mocks. | decided (technical) |
| D8 | Business hours | Mon-Fri 08:00-20:00 (timezone set in `infra/aws/schedule.tf`). EventBridge Scheduler starts/stops RDS and ECS; outside those hours the URL returns 503 and the app shows the hours. `scripts/start.sh` for exceptions. Cost about US$25/month (24/7 would be about US$60). | **decided** |
| D7 | Test scenarios in prod | Mock scenario selection (`success`, `success_with_warnings`, `failure`, `refused` + `code`) per attempt, only for `is_staff` users and only with the flag `ECG_TEST_SCENARIOS_ENABLED=true`. Otherwise the field is rejected. | decided (technical) |

Out of V1 scope: AI fallback (V2), own domain (the `execute-api` URL already has HTTPS; a custom
domain on API Gateway can come later at no extra cost beyond DNS), multi-AZ high availability for the database.

## 3. Attempt state machine (`ExamAttempt.status`)

```
created ──read──▶ reading ──▶ awaiting_validation ──accept──▶ interpreting ──▶ completed
                        │                        │                                │
                        ├──▶ reading_refused    └──reject──▶ user_rejected   └──▶ interpretation_error
                        └──▶ reading_error                                              (retry if retryable)
                             (retry if retryable)
```

- Final states that ask for a **new photo** (new attempt on the same exam): `reading_refused`,
  `user_rejected`, `reading_error`/`interpretation_error` with `retryable=false`.
- `retry` is only accepted in `reading_error` / `interpretation_error` with `retryable=true` (`STORAGE_ERROR`,
  `INTERNAL_ERROR`, `TASK_TIMEOUT`). It reruns the failed phase on the same attempt.
- An attempt stuck in `reading`/`interpreting` beyond `STALE_PROCESSING_SECONDS` becomes the phase error state with code
  `TASK_TIMEOUT` (retryable).
- `Exam.status` mirrors the status of the latest attempt.

## 4. What the UI shows for each outcome

| Phase | Lambda status | Attempt state | Screen / action |
|---|---|---|---|
| Reading | `OK` | `awaiting_validation` | Visual validation: overlay with gridlines + trace, zoom, **Accept** / **Reject** buttons |
| Reading | `OK_WITH_WARNINGS` | `awaiting_validation` | Same, with the warning list highlighted (e.g. "V2 partial") above the buttons |
| Reading | `REFUSED` | `reading_refused` | Reason (`error.message`) + preview with the region highlighted + **Take a new photo** |
| Reading | `ERROR` | `reading_error` | Specific message; **Try again** if `retryable`, otherwise **Take a new photo** |
| Interpretation | `OK` | `completed` | Result: summary (classification, headline, rhythm, HR, findings) + expandable details + disclaimer |
| Interpretation | `OK_WITH_WARNINGS` | `completed` | Same, with the warnings visible at the top of the result |
| Interpretation | `ERROR` | `interpretation_error` | Specific message; **Try again** if `retryable`, otherwise **Take a new photo** |
| Any | network/backend failure (5xx, timeout) | n/a | Generic message + **Try again** (the state is not lost: the app re-fetches the attempt) |
| Any | outside business hours (API Gateway 503, D8) | n/a | Notice "Service available Monday to Friday, 8 am to 8 pm" |

Text rules: the UI shows `message` (coming from the lambda) and drives the flow by `code`/`retryable`,
never by text. The result screen always shows the `disclaimer`.

## 5. Gaps in the current state (2026-10-15)

| Area | Today | Target |
|---|---|---|
| Backend | 1 lambda `ecg-analyser` (`analyze` + `refine`), synchronous, fixed `3x4+1` layout | Contract v1, 2 lambdas, asynchronous, 6 layouts, sex F/M |
| Expo app (`ecg-reader/apps/mobile`, SDK 57) | Only `LoginScreen` and `UploadScreen` | Full flow from section 1 + states from section 4 |
| Legacy web (`ecg-reader/apps/web`, Vite) | 14 pages, partly static, served by CloudFront | Replaced by the Expo app web export |
| Hosting | Django on an Oracle VM behind CloudFront (`E1919M81GCZ1D7`) | ECS Fargate + API Gateway (D3) |
| Database | Neon | RDS PostgreSQL (D4) |
| Secrets | Static AWS key in the VM `.env` | ECS task IAM role + SSM SecureString |
| CI/CD | None | CodePipeline V2 + CodeBuild |
| Monitoring | None | CloudWatch Logs, alarms, SNS, AWS Budget |
