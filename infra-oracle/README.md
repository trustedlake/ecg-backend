# Infra-oracle (Terraform) — ecg-backend

Gerencia a migração deste app da EC2 AWS compartilhada pra VM Oracle Cloud
compartilhada (`147.15.66.165`, mesma onde já rodam Lakeship, `persona`, `content` e
`publy_click_lps`), como container Docker `ecg-backend` na porta **8014** do host.

## CloudFront existente é reapontado manualmente (fora do Terraform)

O app já é público hoje via CloudFront `d1i155fprtk4at.cloudfront.net`
(`E1919M81GCZ1D7`), usado por apps reais (`app.leitorecg.com.br`, etc — ver
`CORS_ALLOWED_ORIGINS`). Essa distribuição é gerenciada pelo terraform ANTIGO deste
repo (`infra/terraform/`), cujo state já está desatualizado (a EC2 dedicada que ele
criava foi terminada; hoje o app roda na EC2 compartilhada, reapontado manualmente).

Pra não arriscar conflito de state com o terraform antigo, **este módulo não
gerencia o CloudFront** — depois que o container aqui estiver validado, o reapontamento
do origin (de `ec2-56-125-136-251...` pra `vm.publy.click:8014`) é feito manualmente via
AWS CLI:

```bash
aws cloudfront get-distribution-config --id E1919M81GCZ1D7 --profile trustedlake > /tmp/cf.json
# editar origin/custom_origin_config pra vm.publy.click:8014, usar o ETag do get acima
aws cloudfront update-distribution --id E1919M81GCZ1D7 --profile trustedlake \
  --distribution-config file:///tmp/cf-edited.json --if-match <ETAG>
```

Domínio continua sendo `d1i155fprtk4at.cloudfront.net` — não muda, então
`ALLOWED_HOSTS`/`CORS_ALLOWED_ORIGINS` do app não precisam mudar.

## Credencial AWS pro boto3 (lambdas + S3)

O app chama 3 lambdas (`ecg-lambdas-lambda-{1,2,3}-...`) e lê/grava em 2 buckets S3
(`ecg-lambdas-images-...`, `ecg-lambdas-results-...`). Na EC2 isso funciona via IAM role
da instância — a VM Oracle não tem IAM role (não é AWS), então precisa de
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` explícitas em `app_env` (mesma decisão já
tomada pro `content`: reusar a `trustedlake-key` existente).

## Uso

```bash
cd infra-oracle
# criar terraform.tfvars com vm_ssh_private_key_path e app_env (DEBUG, ALLOWED_HOSTS,
# DATABASE_URL, DJANGO_SECRET_KEY, AWS_REGION, CORS_ALLOWED_ORIGINS, AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY - mesmas chaves do .env de produção hoje)
terraform init
terraform plan
terraform apply
```

## Como testar (antes de reapontar o CloudFront de verdade)

- Túnel SSH: `ssh -L 8014:localhost:8014 ubuntu@147.15.66.165` e
  `curl -X POST http://localhost:8014/api/auth/login/ -H 'Host: d1i155fprtk4at.cloudfront.net' -H 'Content-Type: application/json' -d '{}'`
  (mesmo health check do `infra/deploy.sh` antigo — espera 200 ou 400).
- Depois de reapontar o CloudFront: `curl https://d1i155fprtk4at.cloudfront.net/...` deve
  responder da VM Oracle.

## Open questions (decisão do usuário)

1. Terraform antigo (`infra/terraform/`) está órfão/com drift real — vale a pena, numa
   etapa futura separada, reconciliar ou apagar esse state (`ecg-lambdas-terraform-state`
   bucket, key `ecg-backend/terraform.tfstate`)?
2. Quando desligar o `ecg-backend.service` antigo na EC2 compartilhada, depois de
   validado.
