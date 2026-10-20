# infra/aws: ECG backend production

Terraform for the architecture in `docs/migration-plan-v1.md` §0. State in
`s3://ecg-lambdas-terraform-state/ecg-backend-prod/terraform.tfstate` (`sa-east-1`).
Buckets and lambdas are **not** managed here (stacks `ecg-lambdas` and `mock_lambdas/infra`).

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
  aws ssm put-parameter --region sa-east-1 --type SecureString --name /ecg/prod/DB_PASSWORD --value '<password>'
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
CodePipeline V2 `ecg-backend` (QUEUED mode): a push to `main` of `tl-gmiler/ecg-backend` **or**
`trustedlake/ecg-reader` -> **Test** (`ci/buildspec-test.yml`) -> **Build** (Expo web export with
`scripts/build_frontend.sh` + ARM image in ECR, tag `<back sha>-<front sha>`) -> **Deploy** (`ci/deploy.sh`).
A pipeline failure sends an email through the SNS topic `ecg-alerts`.

1. **Authorize GitHub (once, after the apply):** the repos are in different accounts and each connection
   sees only one account. AWS console -> Developer Tools -> Settings -> Connections:
   - `ecg-github` (backend, account `tl-gmiler`) -> *Update pending connection* -> install/choose the AWS app
     on the `tl-gmiler` account;
   - `ecg-github-frontend` (app, account `trustedlake`) -> the same, signed in to GitHub as `trustedlake`.
   Until then the pipeline fails at the Source stage.
2. **Trigger by hand:** `aws codepipeline start-pipeline-execution --name ecg-backend`.
3. **What the deploy does:** starts RDS if it is stopped, registers a new task definition revision
   (from the latest one in the family, so Terraform changes land here), runs `migrate` and
   `ensure_admin` in one-off tasks, updates the service, smoke-tests `/api/health/` (new version) and `/`,
   and rolls back to the previous revision if the smoke test fails.
4. **Deploy outside business hours:** the environment is turned on only for the deploy and turned off again at the end
   (ECS at 0, RDS stopping). Within business hours it stays as it is.

## First deploy
The service and autoscaling start with 0 tasks, because the `bootstrap` image does not exist in ECR. The first
pipeline run creates the image and the revision and starts the task (or the 08:00 action starts it the next day).
