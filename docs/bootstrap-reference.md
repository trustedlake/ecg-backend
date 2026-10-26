# Infrastructure bootstrap reference

The short operational guide is [deployment.md](deployment.md). The Python helper
operates existing pipelines; this reference describes the additional setup needed
before those pipelines exist. The checked-in Terraform state backends and resource
names target the competition account `785236377332` in `us-east-1`. Deploying into a
different account requires adapting those values and the helper's account guard.

## Dependencies and state

| Stack | Source | State key |
| --- | --- | --- |
| Backend platform | This repository, `infra/aws` | `ecg-backend-prod/terraform.tfstate` |
| Image/results buckets | Legacy bucket stack, `trustedlake/ecg-lambdas` | `prod/terraform.tfstate` |
| Lambda functions and pipeline | `tl-gmiler/ecg-lambdas-v1`, `lambdas/infra` | `ecg-mock-lambdas/terraform.tfstate` |

State bucket: `ecg-terraform-state-785236377332-us-east-1`, versioned and encrypted.
The bucket and AWS credentials must exist before `terraform init`.
The legacy bucket stack is outside the three application repositories; when
bootstrapping independently, provision the required buckets explicitly instead of
assuming the Lambda stack creates them:

- `ecg-lambdas-images-785236377332-us-east-1`
- `ecg-lambdas-results-785236377332-us-east-1`
- `ecg-lambdas-models-785236377332-us-east-1` (versioned)

Keep image/results buckets private. For browser uploads configure images-bucket
CORS for the application's origin and PUT/GET/HEAD, allowing the upload headers.
The models bucket must contain the approved `model_selector.pkl` matching
`lambdas/real_reading/model.sha256`. Obtain that approved artifact from the project
maintainer; do not retrain it, replace it or change the checksum to bypass validation.
It is not downloaded anonymously from the private production bucket.

## Secrets

Create these SSM Parameter Store **SecureString** parameters with your own values:

| Parameter | Purpose |
| --- | --- |
| `/ecg/prod/DB_PASSWORD` | PostgreSQL master password, user `ecg_admin` |
| `/ecg/prod/DJANGO_SECRET_KEY` | Django secret |
| `/ecg/prod/ADMIN_USERNAME` | Administrator created by `ensure_admin` |
| `/ecg/prod/ADMIN_PASSWORD` | Administrator password |

Use the AWS console or a secure input mechanism; do not paste real values into
committed files. RDS passwords cannot contain `/`, `@`, double quotes or spaces.
The backend Terraform uses write-only RDS password attributes (Terraform >= 1.11,
AWS provider ~> 6.69). To rotate the database password, update SSM and increment
`db_password_version` in a reviewed Terraform change.

## First setup order

1. Provision state, storage buckets, approved model and SSM parameters above.
2. Clone `tl-gmiler/ecg-lambdas-v1`. Prepare the interpretation package from the
   repository root. This Python command works on Windows, macOS and Linux:

   ```text
   python -c "from pathlib import Path; import shutil; p=Path('lambdas/build/interpretation'); p.mkdir(parents=True, exist_ok=True); [shutil.copy2(Path('interpretation')/n,p/n) for n in ('handler.py','diagnoser.py')]; shutil.copytree('lambdas/ecg_contract',p/'ecg_contract',dirs_exist_ok=True)"
   ```

3. In `lambdas/infra`, run `terraform init` and `terraform plan`, supplying
   `-var aws_profile=<profile>` (or an empty value to use the AWS credential chain).
   Review and apply the plan. The initial reading image must already exist in ECR
   with the configured `reading_image_tag`; the legacy default tag is not a
   portable bootstrap artifact. Build/push an image using
   `lambdas/real_reading/Dockerfile` and the context used in `ci/buildspec-build.yml`
   before creating the reading function in a fresh account. Local image builds
   require Docker; normal releases use CodeBuild.
4. In this repository's `infra/aws`, run `terraform init`, then
   `terraform plan -var office_hours_enabled=false -var aws_profile=<profile>`.
   Set `alarm_email` to your address; review the plan and apply with the same
   variables. Set `test_scenarios_enabled=false` if staff mocks must be disabled.
5. In the regional AWS Developer Tools Connections console, authorize `ecg-github`
   for `tl-gmiler` and `ecg-github-frontend` for `trustedlake`. Public repositories
   still require the source connections to be authorized. Confirm the SNS email.
6. From the backend root, run `python deploy.py doctor`, then `python deploy.py deploy`.
   The bootstrap ECS service starts at zero tasks until an application image is
   published. The backend pipeline starts RDS, migrates the database, ensures the
   administrator, starts ECS and checks the release. Verify a full upload in the app.

Terraform ignores pipeline-managed function code and the ECS service task revision
so later applies do not replace a deployed release with bootstrap code.

## Architecture and operation

Backend: ECS Fargate ARM64, cluster `ecg`, service `ecg-backend`, containers `web`
and `worker`; API Gateway HTTP API, VPC Link and Cloud Map; private RDS PostgreSQL;
SQS `ecg-tasks` and DLQ. Reading: Lambda `ecg-reading` (container, ECR `ecg-analyser`).
Interpretation: Lambda `ecg-interpretation` (Python package). OpenCV runs in reading,
not in the Django backend. The backend pipeline builds both backend and Expo web.

Monitoring uses CloudWatch, SNS `ecg-alerts` and an AWS Budget. Production is
always on; always include `office_hours_enabled=false` in backend plans/applies
because the Terraform variable still defaults to business hours.
Task defaults are 512 CPU units and 1024 MB; the next pipeline release picks up
reviewed task definition changes. Refer to `ci/deploy.sh` in each repository for
smoke-check rollback behavior. The helper never destroys infrastructure.
