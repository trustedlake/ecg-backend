# Deployment

## Try the application

[Open ECG Reader](https://7y0j5a8yte.execute-api.us-east-1.amazonaws.com/).
Use the guest credentials supplied with the submission. Create an exam, upload an ECG
photo, review the extracted trace and continue to the result.

## Source code

- [Backend and deployment helper](https://github.com/trustedlake/ecg-backend)
- [Extractor, Selector and interpretation](https://github.com/trustedlake/ecg-lambdas-v1)
- [Expo frontend](https://github.com/trustedlake/ecg-reader)

## Existing AWS environment

Install Python 3.9+, AWS CLI v2, Git and Terraform >= 1.11. No Python packages or
local Docker are required for the helper. Authenticate an AWS identity authorized
for account `785236377332`, region `us-east-1`. Clone the backend and run from its root:

```text
git clone https://github.com/trustedlake/ecg-backend.git
cd ecg-backend
python deploy.py doctor
python deploy.py plan
python deploy.py deploy
python deploy.py status
```

On macOS/Linux use `python3` if `python` is unavailable; on Windows `py -3` also works.
For a named AWS profile append `--profile trustedlake` to each command.

| Command | Result |
| --- | --- |
| `doctor` | Checks tools, Terraform version, AWS account, existing pipelines, state bucket and application health. |
| `plan` | Initializes and previews the backend Terraform stack, explicitly keeping service hours disabled (24/7). Never applies changes. |
| `deploy` | Runs existing Lambda and backend CodePipelines in order and waits for each exact execution; then checks API/database and web access. |
| `status` | Shows the latest executions and checks API/database and web access; exits nonzero if any latest run is not successful. |

`deploy --pipeline ecg-backend` publishes backend plus frontend;
`deploy --pipeline ecg-lambdas` publishes both functions. Default wait is 30 minutes
per pipeline; change with `--timeout 2400`. Timeout or Ctrl+C does not cancel AWS:
inspect `status` and the CodePipeline console before retrying.

Production pipelines read `main` from `trustedlake/ecg-backend`,
`trustedlake/ecg-lambdas-v1` and `trustedlake/ecg-reader` through the authorized
`ecg-github-frontend` AWS connection in `us-east-1`. Pushes to these repositories
trigger their corresponding production pipeline. The helper deploys these remote branches.
An active execution causes the helper to refuse an additional run.

## New environment / infrastructure changes

These commands operate the existing production account. They do not create a fresh
AWS account or duplicate the environment. See [bootstrap reference](bootstrap-reference.md)
for resource dependencies, SSM secrets, state and first-time setup. Infrastructure
changes require review of the Terraform plan and a separate apply. Production has no automatic shutdown schedules.

The AWS GitHub connections require manual authorization, and the SNS alert email
requires manual confirmation. Never commit credentials or Terraform state. Keep the
approved Selector model and its checked-in checksum unchanged.

## Verify and recover

The helper checks `/api/health/` for `status=ok` and `db=ok`, and checks `/` returns
HTTP 200. Complete an upload in the app to verify the full user flow.
The pipelines perform their own tests and smoke checks, with rollback on failed
deployment smoke checks. A failed build leaves the previous release running.
Use CodePipeline action details and CloudWatch logs to investigate a failed run.
For a manual code rollback, revert the offending commit through a PR and let the
pipeline publish it; database migrations may need separate recovery planning.

Production runs 24/7. Staff mock scenarios are controlled by
`test_scenarios_enabled`; set false when a release must disable them.
