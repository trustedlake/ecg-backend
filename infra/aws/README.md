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
terraform plan -var alarm_email=<seu e-mail> -out tf.plan
terraform apply tf.plan      # só com aprovação explícita
```
A senha do banco não vai para o plan nem para o state (`password_wo`). Para trocá-la: atualize o
parâmetro no SSM e rode com `-var db_password_version=<n+1>`.

Depois do primeiro apply, confirme a assinatura do SNS no e-mail recebido (sem isso os alarmes não chegam).

## O que cada arquivo cria
- `network.tf`, `rds.tf`, `ecr.tf`, `sqs.tf`, `iam.tf` (I1): VPC, SGs, RDS, ECR, fila + DLQ, roles das tasks.
- `ecs.tf`: cluster `ecg`, task `ecg-backend` (containers `web` e `worker`), serviço e Cloud Map `backend.ecg.internal`.
- `api.tf`: HTTP API → VPC Link → Cloud Map. A URL fica no output `api_url`.
- `schedule.tf`: autoscaling (1–2 tasks, CPU 70 %) e o horário comercial.
- `monitoring.tf`: SNS `ecg-alerts`, 10 alarmes e o budget mensal (US$35).
- `cicd.tf`: conexão GitHub, 3 projetos CodeBuild, bucket de artefatos e o CodePipeline.

## Horário de funcionamento
Seg–sex, fuso `America/Sao_Paulo`: o RDS liga às 07:40 e desliga às 20:10; o ECS sobe às 08:00 e
vai a 0 às 20:00. Fora disso o API Gateway responde 503 e o app mostra o aviso de horário.

- Exceção pontual: `scripts/ligar.sh` (RDS e depois ECS min 1/max 2) e `scripts/desligar.sh` (o inverso).
  Valem até a próxima ação agendada.
- Sempre ligado: `-var office_hours_enabled=false` remove os agendamentos (custo ≈ US$60/mês). Depois
  rode `scripts/ligar.sh` uma vez, porque o Terraform não mexe no min/max atual do autoscaling.
- Os horários estão nas variáveis `ecs_start_cron`, `ecs_stop_cron`, `rds_start_cron` e `rds_stop_cron`.

## Tamanho da task
`task_cpu` (padrão 512 = 0,5 vCPU) e `task_memory` (1024 MB). Para 1 vCPU: `-var task_cpu=1024 -var
task_memory=2048`. O Terraform registra uma revisão nova da task definition; o serviço só passa a usá-la
no próximo deploy do pipeline (o serviço ignora `task_definition`).

## CI/CD (`cicd.tf`, `ci/`)
CodePipeline V2 `ecg-backend` (modo QUEUED): push na `main` de `tl-gmiler/ecg-backend` **ou**
`trustedlake/leitor-ecg` → **Test** (`ci/buildspec-test.yml`) → **Build** (export web do Expo com
`scripts/build_frontend.sh` + imagem ARM no ECR, tag `<sha back>-<sha front>`) → **Deploy** (`ci/deploy.sh`).
Falha do pipeline manda e-mail pelo SNS `ecg-alerts`.

1. **Autorizar o GitHub (uma vez, depois do apply):** os repos estão em contas diferentes e cada conexão
   enxerga uma só conta. Console AWS → Developer Tools → Settings → Connections:
   - `ecg-github` (backend, conta `tl-gmiler`) → *Update pending connection* → instalar/escolher o app
     da AWS na conta `tl-gmiler`;
   - `ecg-github-frontend` (app, conta `trustedlake`) → o mesmo, logado no GitHub como `trustedlake`.
   Até isso o pipeline falha na etapa Source.
2. **Disparar à mão:** `aws codepipeline start-pipeline-execution --name ecg-backend`.
3. **O que o deploy faz:** liga o RDS se estiver parado, registra uma revisão nova da task definition
   (a partir da última da família, então mudanças do Terraform entram aqui), roda `migrate` e
   `ensure_admin` em tasks avulsas, atualiza o serviço, faz smoke em `/api/health/` (versão nova) e `/`,
   e volta para a revisão anterior se o smoke falhar.
4. **Deploy fora do horário:** o ambiente é ligado só para o deploy e desligado de novo no fim
   (ECS em 0, RDS parando). Dentro do horário fica como está.

## Primeiro deploy
O serviço e o autoscaling nascem com 0 tasks, porque a imagem `bootstrap` não existe no ECR. A primeira
execução do pipeline cria a imagem, a revisão e sobe a task (ou a ação das 08:00 sobe no dia seguinte).
