# End-to-end check of the Lambda outputs (contract 2.0)

Run on 2026-10-22 against the real AWS account (785236377332, `sa-east-1`): the backend code
(`exams.services.run_reading` / `run_interpretation` / `attempt_reading_view`, unchanged) driving the
deployed `ecg-reading` (image `v4-vendored`) and `ecg-interpretation`, with real S3 artifacts and presigned URLs.
Test scenarios use the `mock` field of the prod Lambdas (the `-mock` Lambdas no longer exist).

Not covered here: the deployed HTTP API with a login and the app rendering the images (needs credentials and a
browser). Re-check on a device before release.

## 1. Reading outcomes

| Case | Input | Lambda status | Backend attempt status | Result |
|---|---|---|---|---|
| OK | `mock: success` | `OK` | `awaiting_validation` | 6 methods, `faint_ink` (81) chosen, no warnings |
| OK_WITH_WARNINGS (mock) | `mock: success_with_warnings` | `OK_WITH_WARNINGS` | `awaiting_validation` | chosen method's own warning shown (`AMBIGUOUS_TRACE`, lead V1) |
| OK_WITH_WARNINGS (real) | real photo `smoketest/mi245.jpg`, 48 s | `OK_WITH_WARNINGS` | `awaiting_validation` | chosen `faint_ink` has no warnings, so the backend shows the synthetic `METHODS_PARTIAL_RESULT` (other methods carry `AMBIGUOUS_TRACE` / `LEAD_PARTIAL_TRACE`) |
| REFUSED (mock) | `mock: refused` | `REFUSED` `READ_REFUSED_LOW_CONTRAST` | `reading_refused` | no methods, no preview (mock) |
| REFUSED (real) | real photo that cannot be read, 86 s | `REFUSED` `READ_REFUSED_UNREADABLE_TRACE` | `reading_refused` | 6 methods all `ABSTAINED`, refusal preview URL present |
| ERROR (mock) | `mock: failure` | `ERROR` `READ_TRACE_EXTRACTION_FAILED` | `reading_error` | |
| ERROR (real) | layout `6x2` | `ERROR` `LAYOUT_UNSUPPORTED` | `reading_error` | |
| ERROR (real) | no calibration | `ERROR` `CALIBRATION_INVALID` | `reading_error` | the backend always sends calibration |

`READ_SELECTOR_UNAVAILABLE` cannot be forced (no mock for it); it goes through the same generic `ERROR` path.

## 2. Methods and images

All 6 methods (`baseline_peaks`, `chromatic_ink`, `faint_ink`, `uneven_photo`, `grayscale_contrast`,
`same_appearance`) come back with `files.image`, `files.leads` and `files.json`, in mocks and real runs, and all six
`image_url`s (and the 6 variants) are presigned by `attempt_reading_view`. `ABSTAINED` methods (seen in real runs)
keep their image but are excluded from `variants` and from the choice. The chosen method is the `OK` one with the
highest `selector_score`. Per-method `warnings` is stored for the chosen method (PR #8 behaviour confirmed; the Lambda
no longer sends a top-level `warnings`).

## 3. Interpretation

Called with the chosen `leads.json` as `signal` (plus `sampling_rate_hz` from the chosen method). Results:
`success` -> `completed` + Report; `success_with_warnings` -> `completed`; `failure` with `INTERP_NO_QRS_DETECTED`,
`INTERP_SIGNAL_TOO_NOISY`, `INTERNAL_ERROR` -> `interpretation_error` with the code. A real signal from the photo
also completes (classification `abnormal`, mock interpreter).

## 4. Timeouts

- Real readings took 38 s to 104 s; mock scenarios 2-4 s.
- The HTTP request is not held open: the endpoint answers 202 and the SQS worker calls the Lambda, so the API Gateway
  30 s limit does not apply. There is no load balancer in the path.
- Chain: Lambda 900 s < boto `read_timeout` 930 s < SQS visibility 960 s < stale-processing recovery 1200 s.
  ECS `stopTimeout` is 120 s, so a deploy during a read is recovered by the stale-processing sweep.

## 5. Cleanup

- No `-mock` Lambda exists in `sa-east-1`; the ECS task definition (rev 17, web and worker) points at `ecg-reading` /
  `ecg-interpretation`; the `ecg-ecs-task` IAM policy only allows invoking those two. Nothing to change in AWS.
- The old `INTEGRACAO_ECG.md` no longer exists. Stale `mock_lambdas` mentions in `infra/aws/main.tf` and
  `infra/aws/README.md` were fixed. Remaining `-mock` mentions are historical notes in `docs/`.

## 6. Observations

- Lambda error messages come back in Portuguese and are passed through to the app unchanged (e.g. "Contraste
  insuficiente..."). The project is English-only; worth asking the Lambda owners or mapping messages by code.
- `refine` (manual correction): `ecg-reading` ignores `operation` and treats any request as `read`
  (`operation: refine` returned `LAYOUT_MISSING`). The backend has no `refine` code (V1 decision D2).
