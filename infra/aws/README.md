# infra/aws — produção do backend ECG

Terraform da arquitetura em `docs/plano-migracao-v1.md` §0. State em
`s3://ecg-lambdas-terraform-state/ecg-backend-prod/terraform.tfstate` (`sa-east-1`).
Buckets e lambdas **não** são gerenciados aqui (stacks `ecg-lambdas` e `mock_lambdas/infra`).

## Pré-requisitos
- Terraform ≥ 1.11 (write-only/ephemeral); provider `hashicorp/aws` ~> 6.69.
- Credenciais da conta `785236377332` (profile `trustedlake` por padrão; `-var aws_profile=` para trocar).
- Parâmetros **SecureString** criados à mão (sem valores no git):

  | Nome | Uso |
  |---|---|
  | `/ecg/prod/DB_PASSWORD` | senha do usuário mestre `ecg_admin` do RDS (lida pelo Terraform e pela task) |
  | `/ecg/prod/DJANGO_SECRET_KEY` | `SECRET_KEY` do Django |
  | `/ecg/prod/ADMIN_USERNAME` | superusuário criado por `manage.py ensure_admin` |
  | `/ecg/prod/ADMIN_PASSWORD` | senha desse superusuário |

  ```sh
  aws ssm put-parameter --region sa-east-1 --type SecureString --name /ecg/prod/DB_PASSWORD --value '<senha>'
  ```
  A senha do RDS não pode ter `/`, `@`, `"` nem espaço.

## Comandos
```sh
cd infra/aws
terraform init
terraform plan -out tf.plan
terraform apply tf.plan      # só com aprovação explícita
```
A senha do banco não vai para o plan nem para o state (`password_wo`). Para trocá-la: atualize o
parâmetro no SSM e rode com `-var db_password_version=<n+1>`.
