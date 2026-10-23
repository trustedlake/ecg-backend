# ECG Backend

Django API behind the ECG app (Expo Go / Expo Web). It stores exams and attempts, hands out presigned S3 uploads and runs two lambdas: `ecg-reading` (trace extraction, contract 2.0) and `ecg-interpretation` (mock for now). It does not issue a clinical diagnosis.

The lambda contract lives in [tl-gmiler/ecg-lambdas-v1](https://github.com/tl-gmiler/ecg-lambdas-v1). The user flow and the migration decisions are in `docs/user-flow-v1.md` and `docs/migration-plan-v1.md`.

## Flow

1. `POST /api/auth/login/` returns a token.
2. `POST /api/exams/` creates an exam with the patient data.
3. `POST /api/exams/<exam_id>/attempts/` creates an attempt and returns a presigned PUT (JPG, PNG or WebP).
4. The app uploads the photo straight to S3, then confirms the upload.
5. `.../read/` starts the reading; `.../accept/` or `.../reject/` closes the visual validation; interpretation follows. Each phase answers 202 and the app polls the attempt every 2 s.
6. `.../retry/` restarts the failed phase. Refusing a reading means taking a new photo.

Test scenarios (`success`, `success_with_warnings`, `refused`, `failure`) only work for staff users and with `ECG_TEST_SCENARIOS_ENABLED=true`.

## Production (AWS, us-east-1)

- ECS Fargate (ARM) runs one task with two containers: `web` (gunicorn + WhiteNoise, serving `/api`, `/admin` and the Expo web export) and `worker` (`manage.py ecg_worker`, consuming SQS).
- API Gateway HTTP API → VPC Link → Cloud Map → ECS. No CloudFront, no load balancer.
- RDS PostgreSQL 17 (private, SSL only); SQS `ecg-tasks` with a DLQ; secrets in SSM `/ecg/prod/*`.
- Business hours only (Mon–Fri, America/Sao_Paulo); outside them the API answers 503.
- CI/CD: CodePipeline + CodeBuild (`ci/`): test → build image → deploy (migrations, ECS update, smoke test).
- Infrastructure as code: `infra/aws` (see its README).

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Whole stack on your machine, with fake lambdas and a fake S3 (nothing touches AWS):

```bash
scripts/build_frontend.sh ../leitor-ecg     # Expo web export -> frontend_dist/
python scripts/local_server.py --async 3 --lambdas-repo ../ecg-lambdas-v1
# open http://127.0.0.1:23132/ and sign in as dev / dev12345 (staff)
```

Expo Go on a phone cannot reach the local server; point it to the production API with `EXPO_PUBLIC_API_BASE_URL=<api_url>`.

## Tests

```bash
python manage.py test                                  # lambdas, S3 and SQS mocked
python manage.py check --deploy --fail-level WARNING   # with production settings
```

Clinical use still requires medical validation, personal-data handling and the applicable regulatory processes.
