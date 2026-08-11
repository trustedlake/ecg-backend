# ecg-backend

Backend Django + DRF do Projeto ECG (arquitetura em `../ARCHITECTURE.md`).
**Fase 2**: orquestração das 3 lambdas em 2 fases (Fase A: lambda-1 + lambda-2
sem gate; Fase B: lambda-3 após aprovação) + presigned URLs do S3. Fase 1
entregou: esqueleto, schema, login.

## Stack

- Python 3.12 (mesma das lambdas)
- Django 5.2 LTS + Django REST Framework
- User custom (`AUTH_USER_MODEL = 'accounts.User'`, `USERNAME_FIELD='email'`),
  definido **antes da primeira migration** — não troque depois
- Banco: **Neon Postgres** via `DATABASE_URL` (`django-environ` + driver
  **psycopg v3**); sem `DATABASE_URL`, cai em SQLite local
- Auth: `rest_framework.authtoken` (token opaco, header
  `Authorization: Token <token>`). Login é tela própria do frontend, **não** o
  Django Admin (o Admin existe só para o dono do sistema gerenciar médicos)
- AWS: `boto3` — invocação síncrona das lambdas e presigned URLs

## Como rodar localmente

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # preencha DATABASE_URL
python manage.py migrate
python manage.py runserver
```

## AWS (lambdas + S3)

### Setup da EC2 de produção (Terraform em `infra/terraform/`)

A instância EC2 é provisionada por Terraform (state em
`ecg-lambdas-terraform-state`, key `ecg-backend/terraform.tfstate`, sem tocar o
state do ecg-lambdas). Passos na ordem:

```bash
# 1. (uma vez) Gera o par de chaves SSH localmente — o Terraform NUNCA lida com
#    a privada (senão ela iria em texto claro pro tfstate). Roda só se não
#    existir; idempotente.
./infra/keys/generate-key.sh        # Windows: rodar via git-bash/WSL

# 2. (uma vez) Preenche os segredos no SSM (SecureString). Placeholders criados
#    pelo apply depois são sobrescritos aqui — nada de segredo no terraform/git:
#    aws ssm put-parameter --name /ecg-backend/DATABASE_URL --type SecureString --value "postgresql://..." --overwrite
#    aws ssm put-parameter --name /ecg-backend/DJANGO_SECRET_KEY --type SecureString --value "..." --overwrite

# 3. Plan/apply da infra. SSH fica na instância (porta 22) LIBERADO SÓ pro seu
#    IP — a variável ssh_allowed_cidr não tem default de propósito:
cd infra/terraform
terraform apply -var="ssh_allowed_cidr=<SEU_IP>/32"   # ex.: 203.0.113.42/32
```

Depois do apply, `terraform output instance_public_ip` mostra o IP público; o
deploy da aplicação é manual (espírito do deploy.md do ecg-lambdas):

```bash
./infra/deploy.sh <IP_PUBLICO>
```

O `user_data` do boot já cria o `.env` (DEBUG=False, ALLOWED_HOSTS=IP público,
DATABASE_URL/DJANGO_SECRET_KEY lidas do SSM) e registra o serviço systemd
`ecg-backend` (Gunicorn 0.0.0.0:8000, `--timeout 360` — acima dos 300s das
lambdas da Fase B; sem nginx/ALB, decisão fechada).

## Credenciais dev

- Credenciais: **cadeia padrão do boto3** — para dev local:
  `$env:AWS_PROFILE="trustedlake"` (ou `AWS_PROFILE=trustedlake` no shell)
  com o perfil em `~/.aws/credentials`. Em produção isso vira IAM role da
  instância (fase Terraform). `AWS_PROFILE` também pode ser posto no `.env`.
- Buckets (overridáveis via env): `AWS_IMAGES_BUCKET`, `AWS_RESULTS_BUCKET`.
- Nomes das lambdas (já prefixados com `ecg-lambdas-` — o terraform prefixa com
  `app_name`, então NÃO usar os nomes do `main.tf` como estão): ver defaults em
  `config/settings.py`; overridáveis via env.
- `boto3` client lambda com `Config(read_timeout=330, connect_timeout=10)` —
  lambda-2 tem timeout de 300s e o client precisa aguentar ler a resposta.
- **Upload**: presigned PUT direto do frontend pro S3 (o Django nunca recebe os
  bytes). A chamada ao S3 **não pode** carregar o header `Authorization` do
  token da API — o S3 rejeita assinatura+header juntos (400 InvalidArgument).
- Se `content_type` for enviado na criação da attempt, ele é **assinado** no
  presigned PUT: o frontend DEVE mandar exatamente esse valor no header
  `Content-Type`, senão 403. Sem `content_type`, qualquer conteúdo é aceito.
- Presigned GETs são gerados **sob demanda** a cada resposta — nunca persistidos.

## Endpoints (todos exigem `Authorization: Token ...`)

| Método | Path | Descrição |
|---|---|---|
| POST | `/api/auth/login/` | email+senha → `{token, user}` (sem token) |
| GET | `/api/exams/` | lista exames do médico (paginado, page_size=20) |
| POST | `/api/exams/` | cria exame: `patient_id` OU paciente inline `{name, age, gender}` + `reason` |
| GET | `/api/exams/<id>/` | detalhe + tentativas + report + URLs presigned on-demand |
| POST | `/api/exams/<id>/attempts/` | cria attempt `{filename?, content_type?}` → `{attempt_id, upload_url, imagem_s3_key}` |
| POST | `/api/exams/<id>/attempts/<aid>/process/` | Fase A (lambda-1+2) → overlays + fidelidade |
| POST | `/api/exams/<id>/attempts/<aid>/aprovar/` | Fase B (lambda-3) → report + URLs das 3 imagens |
| POST | `/api/exams/<id>/attempts/<aid>/rejeitar/` | marca rejeitado (reprocessar = nova attempt) |
| POST | `/api/attempts/<aid>/feedback/` | `{issues, description}` |

Transições de estado: `process` exige `criado`; `aprovar`/`rejeitar` exigem
`aguardando_aprovacao` (senão 409). Erro em lambda → attempt/exam `erro` + 502.
`sexo_feminino` não é enviado à lambda-3 (decisão fechada; ela aceita ausência).

## Testes

```bash
# testes unitários usam SQLite (mockam invoke_lambda/presign — nunca tocam AWS)
$env:DATABASE_URL="sqlite:///test.sqlite3"; python manage.py test exams accounts
# smoke E2E com lambdas reais (sobe imagem de teste, Fase A, Fase B, limpa tudo)
$env:AWS_PROFILE="trustedlake"; python manage.py runserver 127.0.0.1:8001
$env:AWS_PROFILE="trustedlake"; python smoke_e2e.py
```

> **`smoke_e2e.py` faz chamadas REAIS na AWS** (invoca as 3 lambdas, sobe imagem
> no S3, cria registros no Neon) — além de criar um usuário temporário no banco.
> **NÃO rodar em CI nem em nenhum ambiente automatizado**: é validação manual
> de ponta a ponta, executada só com a conta de dev (profile `trustedlake`).
> Ele limpa tudo ao final, mas se o processo for morto no meio, pode sobrar
> artefatos no S3/Neon.

## DATABASE_URL

- Local: `DATABASE_URL=sqlite:///db.sqlite3` (ou deixe fora do `.env`)
- Neon: `DATABASE_URL=postgresql://USUARIO:SENHA@host.neon.tech/neondb?sslmode=require`
  (a dev fica em `../leitor-ecg/.env.local`)
- Driver: psycopg 3 (pkg `psycopg[binary]`). IMPORTANTE: o `.env` deve ser
  salvo **sem BOM** (PowerShell 5.1 escreve BOM com `-Encoding UTF8`; use
  ascii/utf8-sem-BOM) — BOM faz o `django-environ` ignorar a linha com erro
  "Invalid line"

## Superuser

```bash
python manage.py createsuperuser   # pede email, name, senha
```

## Login (API)

`POST /api/auth/login/` com `{"email": "...", "password": "..."}` →
`{"token": "...", "user": {"id", "name", "email", "crm"}}`
(vista própria em `accounts/views.py`, não usa o `ObtainAuthToken` do DRF —
mais simples de aceitar `email` e devolver o formato  {token, user} desejado).

## Models

`exams/`: `Patient`, `Exam` (encontro clínico leve), `ExamAttempt` (linha
**imutável** por tentativa de processamento — reprocessar = linha nova),
`Report` (1:1 com a tentativa), `Feedback`. `accounts/`: `User` custom.

> Nota de modelagem (divergências vs. especificação original, decididas lendo
> o código real das lambdas):
> - `ExamAttempt.fidelidade` e `Report.confiabilidade` são **String**
>   (`bom`/`parcial`/`ruim`), não float — a lambda-2/3 devolvem o veredito
>   global da extração.
> - `Report.ritmo` é **JSONField**, não string — a lambda-3 devolve `ritmo`
>   como dict.