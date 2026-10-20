# Infra-oracle (Terraform): ecg-backend

Manages the move of this app from the shared AWS EC2 to the shared Oracle Cloud VM
(`147.15.66.165`, the same one that already runs Lakeship, `persona`, `content` and
`publy_click_lps`), as the Docker container `ecg-backend` on host port **8014**.

## The existing CloudFront is repointed by hand (outside Terraform)

The app is already public through the CloudFront `d1i155fprtk4at.cloudfront.net`
(`E1919M81GCZ1D7`), used by real apps (the web app domains; see
`CORS_ALLOWED_ORIGINS`). That distribution is managed by the OLD terraform in this
repo (`infra/terraform/`), whose state is already stale (the dedicated EC2 it
created was terminated; the app now runs on the shared EC2, repointed by hand).

To avoid a state conflict with the old terraform, **this module does not
manage the CloudFront**: once the container here is validated, the origin
(from `ec2-56-125-136-251...` to `vm.publy.click:8014`) is repointed by hand with the
AWS CLI:

```bash
aws cloudfront get-distribution-config --id E1919M81GCZ1D7 --profile trustedlake > /tmp/cf.json
# edit origin/custom_origin_config to vm.publy.click:8014, use the ETag from the get above
aws cloudfront update-distribution --id E1919M81GCZ1D7 --profile trustedlake \
  --distribution-config file:///tmp/cf-edited.json --if-match <ETAG>
```

The domain stays `d1i155fprtk4at.cloudfront.net`; it does not change, so the app's
`ALLOWED_HOSTS`/`CORS_ALLOWED_ORIGINS` do not need to change.

## AWS credentials for boto3 (lambdas + S3)

The app calls 3 lambdas (`ecg-lambdas-lambda-{1,2,3}-...`) and reads/writes 2 S3 buckets
(`ecg-lambdas-images-...`, `ecg-lambdas-results-...`). On the EC2 this works through the
instance IAM role; the Oracle VM has no IAM role (it is not AWS), so it needs
explicit `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in `app_env` (same decision already
taken for `content`: reuse the existing `trustedlake-key`).

## Usage

```bash
cd infra-oracle
# create terraform.tfvars with vm_ssh_private_key_path and app_env (DEBUG, ALLOWED_HOSTS,
# DATABASE_URL, DJANGO_SECRET_KEY, AWS_REGION, CORS_ALLOWED_ORIGINS, AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY: the same keys as today's production .env)
terraform init
terraform plan
terraform apply
```

## How to test (before actually repointing the CloudFront)

- SSH tunnel: `ssh -L 8014:localhost:8014 ubuntu@147.15.66.165` and
  `curl -X POST http://localhost:8014/api/auth/login/ -H 'Host: d1i155fprtk4at.cloudfront.net' -H 'Content-Type: application/json' -d '{}'`
  (same health check as the old `infra/deploy.sh`; expects 200 or 400).
- After repointing the CloudFront: `curl https://d1i155fprtk4at.cloudfront.net/...` should
  answer from the Oracle VM.

## Open questions (user decision)

1. The old Terraform (`infra/terraform/`) is orphaned with real drift: is it worth, in a separate
   future step, reconciling or deleting that state (`ecg-lambdas-terraform-state`
   bucket, key `ecg-backend/terraform.tfstate`)?
2. When to shut down the old `ecg-backend.service` on the shared EC2, once
   validated.
