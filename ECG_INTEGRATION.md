# ECG integration: single-Lambda contract

Application: Expo Go / Expo Web -> Django (Oracle VM) -> S3 and the AWS Lambda `ecg-analyser`.

| Operation | Input | Expected status | Artifact keys |
|---|---|---|---|
| analyze | `operation`, `attempt_id`, `images_bucket`, `image_s3_key`, `results_bucket`, `calibration:{speed_mm_s,gain_mm_mv}` | `CONSISTENCY_PASS`, `REVIEW_REQUIRED`, `REJECTED_LOW_TRACE_GRID_CONTRAST` | `ecg-analyser/{attempt_id}/initial/{result.json,signals.npz,trace_masks.npz,working_image.png,overview.png}`; a rejection only has `result.json` and `rejection_preview.png` |
| refine | `operation`, `attempt_id`, `results_bucket`, `lead`, `strokes`, `parent_artifacts` | `MANUAL_REVIEW_REQUIRED` | `ecg-analyser/{attempt_id}/{uuid}/{result.json,signals.npz,trace_masks.npz,working_image.png,correction_preview.png}` |

`POST /api/exams/{exam_id}/attempts/{attempt_id}/process/` invokes **synchronously** via boto3 `RequestResponse`. `GET` reads the stored state; it does not poll the Lambda runtime. There is no async queue today. Run time per photo has **not been measured on AWS**. The reading Lambda `ecg-reading` (contract 2.0, `AWS_LAMBDA_ECG_READING`) runs up to 900 s; boto3 client read_timeout 930 s (`AWS_LAMBDA_READ_TIMEOUT`); Gunicorn timeout 960 s (Dockerfile) and proxy read_timeout >= 1000 s, with CDN/LB and client windows to match. Some proxies/CDNs have a lower hard limit: in those environments, move to an async job (SQS/Lambda + status in the DB). Cold start eats into that window.

Approval confirms the **extraction**, not a clinical report. A contrast rejection blocks refine/approval. Manual changes keep review required. Refine uses a state transition to block concurrent calls; if the Django process dies during the Lambda call, the attempt stays in the reading state; after `STALE_PROCESSING_SECONDS` (default 600 s, longer than the Gunicorn timeout) the next access recovers it: back to awaiting validation if an analysis was already saved (interrupted refine), or to an error (interrupted analysis; capture a new photo).

`infra-oracle/` is the target deployment for Django (Oracle VM). The EC2/Terraform infrastructure in `infra/` is legacy: do not reapply it. Infrastructure files are not part of a backend patch.

## Two-lambda v1 contract (reading + interpretation)

The new flow splits **reading** (image + layout -> signal + overlay with gridlines, which may refuse the photo) and
**interpretation** (signal + sex -> summary + details). The contract lives in the [ecg-lambdas-v1](https://github.com/tl-gmiler/ecg-lambdas-v1) repo.
The mock lambdas `ecg-reading-mock` and `ecg-interpretation-mock` ([`mock_lambdas/`](https://github.com/tl-gmiler/ecg-lambdas-v1/tree/main/mock_lambdas), same repo) implement it with synthetic
results and selectable scenarios, to validate the flow before the real scripts. The backend still calls
`ecg-analyser`; the switch happens in the migration card.
