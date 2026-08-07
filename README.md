# ecg-backend

Backend Django + DRF do Projeto ECG (arquitetura em `../ARCHITECTURE.md`).
Escopo desta fase: **esqueleto + schema + login** — sem endpoints de exame,
sem invocação de lambdas, sem presigned URLs (fases seguintes).

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

## Como rodar localmente

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # preencha DATABASE_URL
python manage.py migrate
python manage.py runserver
```

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