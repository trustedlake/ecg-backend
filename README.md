# ECG Backend: Django + single Lambda

This server backs the **Expo Go / Expo Web** frontend. It uses the ECG Analyser as a single Lambda for `analyze` and `refine`. It does not return an automatic clinical diagnosis.

## Flow

1. `POST /api/auth/login/` takes `username` and `password` and returns a `token` (DRF).
2. `POST /api/exams/` creates the exam and the patient data.
3. `POST /api/exams/<exam_id>/attempts/` creates an attempt with `filename`, `content_type`, `speed_mm_s` and `gain_mm_mv`; returns a presigned PUT. Formats: JPG, PNG, WebP.
4. Expo uploads **one photo** to S3 with a `PUT` whose `Content-Type` matches the signed one.
5. `POST /api/exams/<exam_id>/attempts/<attempt_id>/process/` calls the `analyze` Lambda and stores the artifacts; a `GET` on the same route returns the state without rerunning.
6. `POST .../refine/` sends a single lead and include/exclude brush strokes; the Lambda produces a new version without deleting earlier files. The image used for painting is the working image.
7. `POST .../reject/` rejects a result awaiting review; `POST .../accept/` confirms **the trace extraction**, not a medical report.
8. `GET /api/exams/<exam_id>/` lists history and result; `POST /api/attempts/<attempt_id>/feedback/` records feedback.

If the Lambda rejects an image for insufficient contrast, that attempt cannot be refined/approved: capture a new image.

## Setup

Requirements: Python 3.12+, Django 5.2, REST Framework, AWS Boto3, PostgreSQL, or SQLite for development.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Outside development, set `DJANGO_SECRET_KEY`, `DATABASE_URL`, `AWS_REGION`, `AWS_IMAGES_BUCKET`, `AWS_RESULTS_BUCKET`, `AWS_LAMBDA_ECG_ANALYSER`, the `CORS_ALLOWED_ORIGINS` origins, `ALLOWED_HOSTS` and the AWS credentials/IAM. **Do not use `DEBUG=True` in production.** A configuration without a real secret is rejected with `DEBUG=False`.

For Expo Web, allow the browser's exact origin in Django and in the S3 CORS. For Expo Go on a phone, use the local network IP or an HTTPS domain the phone can reach. `localhost` refers to the phone itself.

The Lambda needs `s3:GetObject` on both buckets and `s3:PutObject` on the results bucket; Django needs invoke permissions and presigned URL generation. The deploy image is built with the ECG Analyser `Dockerfile.lambda`. **No AWS deploy is done from the ZIP.**

## Web app and local backend (V1)

The Expo app web export (repo `ecg-reader`) is served by Django itself, on the same origin as the API: WhiteNoise serves `frontend_dist/` and the catch-all in `config/spa.py` returns `index.html` for any route outside `/api`, `/admin` and `/static` (reloading on an inner screen, such as `/exams/<id>/attempts/<id>`, opens the app there).

```bash
scripts/build_frontend.sh ../ecg-reader        # npm ci + expo export --platform web -> frontend_dist/
python scripts/local_server.py --async 3 --lambdas-repo ../ecg-lambdas-v1
# Django + fake lambdas + fake S3, no AWS; login dev / dev12345
```

The local reading is a contract 2.0 fake (`scripts/local_lambdas.py`, with the `mock` field scenarios); the interpretation is the mock handler from a clone of [tl-gmiler/ecg-lambdas-v1](https://github.com/tl-gmiler/ecg-lambdas-v1).

Open `http://127.0.0.1:23132/`. The local backend only listens on `127.0.0.1` (the upload URLs point there too), so it is for the web version; Expo Go on a phone points to the prod API with `EXPO_PUBLIC_API_BASE_URL=<api_url>`. In production, the `frontend_dist/` build runs in CodeBuild (`ci/buildspec-build.yml`).

## Tests

```bash
python manage.py test                     # backend suite (lambdas, S3 and SQS mocked)
python manage.py check --deploy --fail-level WARNING  # after configuring production
```

The `manage.py test` suite and real AWS runs need installs and services that were not available in the audit environment. Clinical use still requires medical validation, personal data handling and the appropriate regulatory processes.

See `ECG_INTEGRATION.md` for contracts and further limits. The v1 contract of the reading and interpretation lambdas (and the mock lambdas that implement it) lives in the [tl-gmiler/ecg-lambdas-v1](https://github.com/tl-gmiler/ecg-lambdas-v1) repo.


## Single-Lambda update (not run on AWS)

See `ECG_INTEGRATION.md`. Django runs on the Oracle VM; S3/Lambda stay on AWS. `smoke_e2e.py` only runs with explicit authorization, creates data and needs a real test photo. Configure Gunicorn/proxy for a long synchronous POST, or implement an async queue. Validate migrations and `manage.py check --deploy` in the production environment.
