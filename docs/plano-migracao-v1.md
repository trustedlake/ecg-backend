# Plano de execução — Migração front/back/infra para Expo Go + CI/CD AWS (V1)

> Escrito em 2026-10-15 para ser executado **etapa por etapa** (ex.: com Sonnet 5.5). Cada etapa é um prompt
> autocontido: copie o bloco "Prompt" inteiro numa sessão nova. Espere o relatório de uma etapa antes de
> rodar a próxima (as etapas dependem do que foi de fato implementado, não do que foi planejado).
>
> Decisões e fluxo do usuário: [`fluxo-usuario-v1.md`](fluxo-usuario-v1.md) (leitura obrigatória em toda etapa).
> Contrato das lambdas: [`../CONTRATO_LAMBDAS.md`](../CONTRATO_LAMBDAS.md).

## 0. Contexto comum (vale para todas as etapas)

**Repositórios (nesta máquina):**
- Backend: `/Users/gustavomiler/workspace/trabalho/projeto-ecg/ecg-backend` (Django 5.2 + DRF, token auth).
  O trabalho deste card está na branch `tlk/card-58ed1492-bf5c-431f-b4ae-dfe6787488c9-5a5c322e99`
  (worktree do card), que já contém `CONTRATO_LAMBDAS.md` e `mock_lambdas/`.
- Frontend: `/Users/gustavomiler/workspace/trabalho/projeto-ecg/leitor-ecg` (monorepo npm workspaces:
  `apps/mobile` = Expo SDK 57 / RN 0.86 / React 19, `packages/shared` = componentes + `createApiClient`,
  `apps/web` = Vite legado, será aposentado). Antes de escrever código Expo, ler a doc versionada
  `https://docs.expo.dev/versions/v57.0.0/` (regra do `apps/mobile/AGENTS.md`). Trabalhar na branch
  `feat/expo-v1-flow` (criar a partir de `main`).
- Lambdas: ~~`ecg-reading-mock` e `ecg-interpretation-mock`~~ → desde a B6, `ecg-reading` (real, contrato 2.0) e
  `ecg-interpretation` (mock), em `sa-east-1` (conta `785236377332`); cenários de teste pelo campo `mock`.
  Buckets: imagens `ecg-lambdas-images-785236377332-sa-east-1`, resultados `ecg-lambdas-results-785236377332-sa-east-1`
  (gerenciados pelo Terraform do repo `ecg-lambdas`, **não** por este repo).

**Arquitetura alvo (decidida em 2026-10-15, não rediscutir):**
```
Expo Go (celular) ─┐
Navegador (web) ───┴─HTTPS─▶ API Gateway HTTP API (URL execute-api, throttling, access log)
                               └─ VPC Link ─▶ Cloud Map (DNS privado) ─▶ ECS Fargate ARM (serviço ecg-backend)
                                                                         task: container "web" (gunicorn, Django + WhiteNoise:
                                                                               /api/*, /admin/ e o export web do Expo)
                                                                             + container "worker" (manage.py ecg_worker,
                                                                               consome a fila SQS)
  web ──▶ SQS ecg-tasks (+ DLQ) ──▶ worker ──▶ Lambda ecg-reading (2.0) / ecg-interpretation (mock)
  web/worker ──▶ RDS PostgreSQL db.t4g.micro Single-AZ (subnet privada, TLS)
  web/worker ──▶ S3 (presigned PUT da foto, GET dos artefatos)
  segredos: SSM Parameter Store SecureString /ecg/prod/* injetados pelo ECS (task definition "secrets")
Rede: VPC própria, 2 subnets públicas (tasks Fargate com IP público só para saída; SG aceita entrada só do
      SG do VPC Link) + 2 subnets privadas sem rota para a internet (RDS). Sem NAT.
Horário: EventBridge Scheduler (America/Sao_Paulo, seg–sex) liga o RDS 07:40 e o ECS 08:00; desliga o ECS
      20:00 e o RDS 20:10. Fora disso a URL devolve 503.
CI/CD: CodePipeline V2 (GitHub: ecg-backend + leitor-ecg) → CodeBuild ARM test → build (imagem no ECR) →
      deploy (migrate em task avulsa → update-service com circuit breaker → smoke → rollback se falhar)
Observabilidade: CloudWatch Logs (14 dias), 10 alarmes → SNS ecg-alerts (e-mail), AWS Budget US$35/mês
```
Proibido: CloudFront, Amplify Hosting, ALB/NLB, NAT Gateway, EC2 gerenciado à mão, Aurora, RDS Multi-AZ,
endpoints de VPC (interface), IP elástico, Container Insights, Secrets Manager (usar SSM). Meta de custo:
≈ US$25/mês com o desligamento noturno (estimativa na §0.2).

**Regras para quem executa:**
- Não fazer `terraform apply`, deploy, `aws ... create/update/delete` nem alterar recursos reais sem
  confirmação explícita do Gus naquela etapa. `terraform plan` e chamadas AWS só de leitura são permitidos.
- Não commitar segredos. Não fazer push nem abrir PR sem pedir.
- Commits pequenos, mensagem em inglês no padrão `feat: …` / `fix: …` / `chore: …`.
- Mudança de comportamento sem teste cobrindo = etapa reprovada.
- Ao final de toda etapa, devolver o **relatório** pedido no fim do prompt.

### 0.1 Contrato da API do backend (V1) — fonte da verdade para back e front

Autenticação: `Authorization: Token <token>` (DRF). Erros de validação: 400 com o formato padrão do DRF.
Erros de estado: 409 `{"detail": "…", "code": "invalid_state"}`.

| Método e rota | Auth | Corpo | Resposta |
|---|---|---|---|
| `GET /api/health/` | não | — | `200 {"status":"ok","db":"ok","version":"<git sha>"}` (503 se o banco falhar) |
| `POST /api/auth/login/` | não | `{username, password}` | `{token, user:{id, username, is_staff}}` (já existe; incluir `is_staff`) |
| `GET /api/config/` | sim | — | `{layouts:[…6], calibration:{speed_mm_s:[12.5,25,50], gain_mm_mv:[5,10,20], default:{speed_mm_s:25, gain_mm_mv:10}}, test_scenarios_enabled: bool}` (`true` só se a flag estiver ligada **e** o usuário for staff) |
| `GET /api/exams/` | sim | — | paginado, cada item = `Exam` resumido |
| `POST /api/exams/` | sim | `{patient_id}` **ou** `{name, age, sex:"F"\|"M"}`, `reason?` | `201 Exam` |
| `GET /api/exams/<exam_id>/` | sim | — | `Exam` com `attempts[]` (cada um = `Attempt` completo) |
| `POST /api/exams/<exam_id>/attempts/` | sim | `{filename, content_type: image/jpeg\|image/png\|image/webp, layout, speed_mm_s?, gain_mm_mv?, test_scenario?}` | `201 {attempt_id, upload_url, upload_headers:{"Content-Type": …}}` |
| `POST /api/exams/<exam_id>/attempts/<id>/read/` | sim | — | `202 Attempt` (`reading`). 409 se não estiver em `created` |
| `GET /api/exams/<exam_id>/attempts/<id>/` | sim | — | `Attempt` (é o endpoint de polling) |
| `POST …/attempts/<id>/accept/` | sim | — | `202 Attempt` (`interpreting`). 409 se não estiver em `awaiting_validation` |
| `POST …/attempts/<id>/reject/` | sim | — | `200 Attempt` (`user_rejected`). 409 idem |
| `POST …/attempts/<id>/retry/` | sim | — | `202 Attempt`. Só em `reading_error`/`interpretation_error` com `retryable=true`; senão 409 |
| `POST /api/attempts/<id>/feedback/` | sim | `{issues[], description}` | já existe, manter |

`test_scenario` (opcional): `{"read": {"scenario": "success|success_with_warnings|failure|refused", "code"?: "…"},
"interpret": {"scenario": "success|success_with_warnings|failure", "code"?: "…"}}`. Aceito só se
`ECG_TEST_SCENARIOS_ENABLED=true` **e** `request.user.is_staff`; caso contrário 400. É repassado como `mock` no
payload da lambda correspondente.

Objeto `Attempt`:
```json
{
  "id": "uuid", "exam_id": "uuid", "status": "<estado da seção 3 do fluxo>",
  "layout": "3x4+1", "calibration": {"speed_mm_s": 25, "gain_mm_mv": 10},
  "original_image_url": "presigned GET | null",
  "reading": null | {
    "status": "OK|OK_WITH_WARNINGS|REFUSED|ERROR",
    "warnings": [{"code","message","lead?"}],
    "error": null | {"code","category","retryable","message"},
    "overlay_url": "presigned GET | null",
    "refusal_preview_url": "presigned GET | null",
    "refusal_region_px": [x0,y0,x1,y1] | null,
    "leads": [{"name","kind","quality","coverage"}]
  },
  "interpretation": null | {
    "status": "OK|OK_WITH_WARNINGS|ERROR",
    "warnings": [...], "error": null | {...},
    "summary": {...contrato 3.2...} | null, "details": {...} | null, "disclaimer": "…" | null
  },
  "can_retry": bool, "needs_new_photo": bool,
  "created_at": "...", "updated_at": "..."
}
```
URLs presigned são geradas a cada resposta e **nunca** gravadas no banco. Amostras do sinal **nunca** vão para
o app (ficam no S3, `artifacts.signal`).

### 0.2 Estimativa de custo (preços `sa-east-1` da API de preços da AWS, 2026-10-15, sem impostos)

Horário ligado: seg–sex 08–20h ≈ 264 h/mês.

| Item | Ligado só no horário | 24/7 (referência) |
|---|---|---|
| Fargate ARM 0,5 vCPU / 1 GB (US$0,0557/vCPU-h + US$0,00612/GB-h) | $8,97 | $24,80 |
| IPv4 público da task (US$0,005/h) | $1,32 | $3,65 |
| RDS `db.t4g.micro` Single-AZ (US$0,034/h) | $8,98 | $24,82 |
| RDS gp3 20 GB (US$0,219/GB-mês, cobrado mesmo parado) | $4,38 | $4,38 |
| Cloud Map DNS privado (zona Route 53) | $0,50 | $0,50 |
| API Gateway HTTP API (US$1,59/milhão), ECR, Logs, SQS, Scheduler, S3, lambdas mock | ~$1,10 | ~$1,10 |
| CodePipeline V2 + CodeBuild ARM (100 min grátis/mês cada) | ~$0–1 | ~$0–1 |
| **Total** | **≈ US$25–26** | **≈ US$59–60** |

Variável `task_cpu`: com 256 (0,25 vCPU) o Fargate cai para ~$5,29 (total ≈ US$22).

---

## Ordem e dependências

| Etapa | Repo | Depende de | Pode rodar em paralelo com |
|---|---|---|---|
| B1 Modelos, estados e validação | backend | — | F1 |
| B2 Cliente do contrato das lambdas | backend | B1 | F1 |
| B3 Tarefas assíncronas + views/rotas | backend | B2 | F1, F2 |
| B4 Container (Docker), worker SQS, settings de prod, SPA | backend | B3 | F2, F3 |
| F1 Cliente da API e tipos (`shared`) | frontend | §0.1 | B1–B3 |
| F2 Telas: login → exame → foto → layout → processando | frontend | F1 | B3, B4 |
| F3 Telas: validação, recusa, erro, resultado, histórico | frontend | F2 | B4 |
| F4 Export web servido pelo Django + verificação local ponta a ponta | ambos | B4, F3 | — |
| I1 Terraform: rede, RDS, ECR, SQS, IAM (só plan) | backend | B4 | F2–F4 |
| I2 Terraform: ECS, API Gateway, horário, monitoramento (só plan) | backend | I1 | F2–F4 |
| I3 CI/CD: CodePipeline V2 + CodeBuild (só plan) | backend | I2, F4 | — |
| B6 Leitura no contrato 2.0 (`ecg-reading`) + merge da `main` | ambos | I3, F4 | — |
| D1 Primeiro deploy em prod (**com o Gus**) | — | B6 + parâmetros SSM criados | — |
| E1 Teste ponta a ponta em prod (matriz completa) | — | D1 | — |
| C1 Limpeza do legado (**com o Gus**) | ambos | E1 | — |

---

## B1 — Modelos, estados e validação (backend)

**Prompt:**

> Repo: `ecg-backend`, branch `tlk/card-58ed1492-bf5c-431f-b4ae-dfe6787488c9-5a5c322e99`. Leia antes:
> `docs/plano-migracao-v1.md` (seções 0 e 0.1), `docs/fluxo-usuario-v1.md` (seções 2–4), `CONTRATO_LAMBDAS.md`
> (seções 2.1, 2.2, 3.1). O backend hoje integra uma lambda única `ecg-analyser` (`analyze` + `refine`) e vai
> passar a usar as lambdas de leitura e interpretação do contrato v1. Esta etapa só mexe em **modelos,
> migrations, serializers de entrada e remoção do refine** — a integração com as lambdas e as views novas são
> as etapas B2/B3 (não implemente).
>
> 1. **Remover o refine** (decisão D2): `AttemptRefineView` e os serializers de pincel em `exams/views.py`, a
>    rota `refine/` em `exams/urls.py`, `run_refine` em `exams/services.py`, `exams/test_refine_contract.py`.
>    Remover `AWS_LAMBDA_ECG_ANALYSER` de `config/settings.py` e criar `AWS_LAMBDA_ECG_READING`
>    (padrão `ecg-reading-mock`) e `AWS_LAMBDA_ECG_INTERPRETATION` (padrão `ecg-interpretation-mock`), mais
>    `ECG_TEST_SCENARIOS_ENABLED` (bool, padrão False).
> 2. **Estados** (`ExamStatus`): substituir pelos da seção 3 do fluxo: `created`, `reading`,
>    `awaiting_validation`, `reading_refused`, `reading_error`, `user_rejected`,
>    `interpreting`, `completed`, `interpretation_error`. Migration de dados mapeando os antigos:
>    `fase_a_processando→reading_error`, `aguardando_aprovacao→awaiting_validation`,
>    `aprovado|completed→completed`, `rejeitado→user_rejected`, `fase_b_processando→interpretation_error`,
>    `erro→reading_error` (em `Exam` e `ExamAttempt`).
> 3. **`Patient`**: trocar `gender` por `sex` (`CharField(max_length=1, choices F/M, null=True)` — nulo só para
>    legado). Migration de dados: `F/f/feminino/female/mulher→F`, `M/m/masculino/male/homem→M`, resto→NULL.
> 4. **`ExamAttempt`**: adicionar `layout` (choices = 6 layouts do contrato 2.2), `reading` (JSONField null),
>    `interpretation` (JSONField null), `test_scenario` (JSONField null). Remover os campos do pipeline antigo:
>    `image_type`, `mascara_s3_key`, `overlay_trace_s3_key`, `overlay_signal_s3_key`, `sinais`, `lead_labels`,
>    `rhythm_index`, `fidelidade`, `fidelidade_por_derivacao` e o enum `FidelityVerdict`. Manter
>    `image_s3_key` e `calibration`. Remover `Exam.ecg_layout` (o layout passa a ser por tentativa).
>    Formato que B2 vai gravar em `reading`: `{status, warnings, error, meta, grid, leads:[{name,kind,quality,coverage}],
>    artifacts:{overlay:{bucket,key}, signal:{bucket,key}, refusal_preview:{bucket,key}|null}, refusal_region_px}`;
>    em `interpretation`: `{status, warnings, error, meta}`. Documente isso num docstring do modelo.
> 5. **`Report`**: substituir os campos antigos por `classification` (normal/borderline/abnormal, null),
>    `summary` (JSON), `details` (JSON), `disclaimer` (Text), `warnings` (JSON, default list), `engine`,
>    `engine_version`. Mantém `OneToOne` com a tentativa.
> 6. **Serializers de entrada** (`exams/serializers.py`): `ExamCreateSerializer` aceita `patient_id` **ou**
>    `{name, age (0–130), sex F|M}` (exatamente um dos dois) + `reason` opcional. `AttemptCreateSerializer`:
>    `filename`, `content_type` ∈ {image/jpeg, image/png, image/webp} (extensão coerente), `layout` obrigatório
>    (6 valores), `speed_mm_s` ∈ {12.5, 25, 50} padrão 25, `gain_mm_mv` ∈ {5, 10, 20} padrão 10,
>    `test_scenario` opcional validado contra a tabela da seção 4 do contrato (cenário `refused` não é aceito em
>    `interpret`); se vier e (`ECG_TEST_SCENARIOS_ENABLED` for False ou o usuário não for staff) → erro 400
>    no campo `test_scenario`. Ajuste a view de criação de tentativa para gravar `layout` e `test_scenario` e
>    para devolver `upload_headers`. Serializers de saída podem ficar mínimos/provisórios (B3 os refaz), mas o
>    projeto precisa subir.
> 7. Atualizar `exams/admin.py` e `tests/test_integration_standalone.py` (remover o que testava o pipeline antigo).
>    Reescrever `exams/tests.py` para os modelos/serializers novos (criação de exame com sexo, validações de
>    layout/calibração/content-type, gate do `test_scenario` staff × não staff × flag desligada, migrations de
>    dados de status e sexo). `exams/test_single_lambda_contract.py` testa o contrato antigo: remova.
>
> Critérios de aceite: `python manage.py makemigrations --check --dry-run` sem pendências;
> `python manage.py test` verde; `python -m unittest discover -s mock_lambdas/tests` continua verde;
> `grep -rn "ecg-analyser\|refine\|fidelidade" exams config` só retorna o que for intencional (explique).
>
> **Relatório a devolver:** lista de arquivos alterados/removidos; o conteúdo das migrations geradas (nomes e
> operações); como ficou a validação do `test_scenario`; qualquer ponto em que este prompt foi ambíguo e a
> decisão que você tomou; saída resumida dos testes (quantos, quais novos).

---

## B2 — Cliente do contrato das lambdas (backend)

**Prompt:**

> Repo: `ecg-backend`, branch do card (ver `docs/plano-migracao-v1.md` §0). Leia antes: plano §0/§0.1,
> `docs/fluxo-usuario-v1.md` §3–4, `CONTRATO_LAMBDAS.md` inteiro, `mock_lambdas/ecg_contract/__init__.py`
> (catálogo de códigos). A etapa B1 já trocou os modelos: `ExamAttempt` tem `layout`, `calibration`,
> `image_s3_key`, `reading` (JSON), `interpretation` (JSON), `test_scenario` (JSON); `Patient.sex` é F/M;
> `Report` tem `classification`, `summary`, `details`, `disclaimer`, `warnings`, `engine`, `engine_version`;
> os estados são os da seção 3 do fluxo. Confira o código real antes de começar.
>
> Objetivo: reescrever `exams/services.py` para falar o contrato v1. **Sem views e sem assíncrono** (isso é B3).
>
> 1. Manter `presign_put_url`/`presign_get_url`/`_s3_client` (SigV4). Remover tudo do `ecg-analyser`
>    (`run_phase_a`, `run_phase_b`, `overlay_urls` antigos).
> 2. `invoke_contract(function_name, payload) -> dict` que **sempre** devolve um envelope do contrato:
>    - resposta normal: validar `status` ∈ {OK, OK_WITH_WARNINGS, REFUSED, ERROR}, `warnings` lista, `error`
>      com `code`/`category`/`retryable`/`message` quando status é REFUSED/ERROR, `result` objeto quando OK*.
>      Envelope malformado → envelope sintético `ERROR` `INTERNAL_ERROR` retryable (logar o corpo truncado).
>    - `FunctionError` (timeout/OOM/exceção não tratada) ou exceção do boto3 → envelope sintético `ERROR`
>      `INTERNAL_ERROR`, `retryable: true`, mensagem pt-BR genérica. Timeout de leitura do cliente =
>      `AWS_LAMBDA_READ_TIMEOUT`, `retries.max_attempts=0` (não reinvocar sozinho).
> 3. `run_reading(attempt)`: monta o payload 2.1 (`request_id=str(attempt.id)`, `image={bucket: AWS_IMAGES_BUCKET,
>    key: attempt.image_s3_key}`, `layout`, `calibration`, `mock` = `attempt.test_scenario["read"]` só se
>    existir), invoca `AWS_LAMBDA_ECG_READING` e aplica o resultado:
>    - OK/OK_WITH_WARNINGS → valida que `artifacts.overlay` e `artifacts.signal` estão no bucket
>      `AWS_RESULTS_BUCKET` e com prefixo `reading/<attempt.id>/` (senão trata como `INTERNAL_ERROR`); grava em
>      `attempt.reading` o resumo (sem `samples`!) e muda para `awaiting_validation`.
>    - REFUSED → grava `error` + `details.preview` (mesma validação de bucket/prefixo) + `details.region_px`;
>      estado `reading_refused`.
>    - ERROR → grava `error`; estado `reading_error`.
> 4. `run_interpretation(attempt)`: só a partir de leitura OK*. Baixa `artifacts.signal` do S3 (JSON), monta o
>    payload 3.1 com `patient.sex = attempt.exam.patient.sex` (se nulo → `ERROR` local `SEX_MISSING`, sem
>    invocar) e `mock = test_scenario["interpret"]` se houver. OK* → cria/atualiza `Report` e estado
>    `completed`; ERROR → `interpretation_error`. Grava `attempt.interpretation`. Falha ao baixar o sinal →
>    `STORAGE_ERROR` retryable.
> 5. Funções de leitura para a API: `attempt_reading_view(attempt)` e `attempt_interpretation_view(attempt)` que
>    devolvem os blocos `reading`/`interpretation` do objeto `Attempt` do §0.1, gerando as presigned URLs na
>    hora; e `attempt_flags(attempt) -> {can_retry, needs_new_photo}` seguindo o fluxo §3.
> 6. Mudanças de estado: atualize `attempt.status` e `exam.status` juntos (o exam espelha a tentativa), em
>    transação.
> 7. Testes (`exams/test_services.py`), com o cliente boto3 mockado e fixtures JSON montadas a partir dos
>    exemplos do contrato: os 4 status da leitura, os 3 da interpretação, FunctionError, exceção de rede,
>    envelope malformado, artefato fora do prefixo, sexo nulo, falha ao baixar o sinal, `mock` só presente
>    quando há `test_scenario`, `samples` nunca persistido no banco, M e F chegando no payload.
>
> Critérios de aceite: `python manage.py test` verde; nenhuma referência a `ecg-analyser` sobrando.
>
> **Relatório a devolver:** assinatura das funções públicas de `services.py`; tabela "status/código da lambda →
> estado da tentativa → can_retry/needs_new_photo" como implementada; trechos exatos da validação de
> bucket/prefixo; decisões onde o contrato deixava margem; lista dos testes novos.

---

## B3 — Tarefas assíncronas + views e rotas (backend)

**Prompt:**

> Repo: `ecg-backend`, branch do card. Leia antes: plano §0/§0.1 (o contrato da API é a fonte da verdade
> desta etapa), fluxo §3–4. Estado atual: B1 trocou modelos/estados; B2 criou em `exams/services.py`
> `run_reading(attempt)`, `run_interpretation(attempt)`, `attempt_reading_view`, `attempt_interpretation_view`,
> `attempt_flags` (confira os nomes reais no código). Agora: execução assíncrona, views e rotas.
>
> 1. `exams/tasks.py`:
>    - `dispatch(task, attempt_id)` com `task` ∈ {`read`, `interpret`}. `settings.ECG_TASK_MODE`:
>      `inline` (padrão; executa na hora, usado em dev e testes) ou `sqs` (usa boto3 `sqs.send_message` na fila
>      `settings.ECG_TASK_QUEUE_URL`, env `ECG_TASK_QUEUE_URL`, corpo JSON `{"ecg_task": task, "attempt_id": "<uuid>"}`;
>      quem consome é o worker da etapa B4, que roda em outro container). Se o dispatch falhar,
>      a tentativa vai para `reading_error`/`interpretation_error` com `INTERNAL_ERROR` retryable.
>    - `run_task(task, attempt_id)`: carrega a tentativa e só executa se ela estiver no estado `*_processando`
>      correspondente (idempotente: evento duplicado não roda duas vezes). Chama `run_reading`/`run_interpretation`.
>      Nunca levanta exceção para fora (loga e marca erro).
> 2. Views e rotas exatamente como §0.1: `read/`, `GET attempt`, `accept/`, `reject/`, `retry/`, `GET /api/config/`,
>    `GET /api/health/` (sem auth; `SELECT 1`; `version` vem de `settings.APP_VERSION`, env `APP_VERSION`, padrão
>    `dev`), e `is_staff` no login. Transições com compare-and-swap (`filter(status=…).update(…)`), como o código
>    atual já faz, para que dois toques não disparem duas tarefas. `retry` volta para o `*_processando` da fase
>    que falhou e despacha a tarefa. Serializers de saída `Exam`/`Attempt` no formato do §0.1. Remover as views e
>    rotas antigas (`process/`, `aprovar/`, `rejeitar/`).
> 3. Recuperação de tentativa presa: adaptar `_recover_stale_attempt` — `*_processando` há mais que
>    `STALE_PROCESSING_SECONDS` vira `erro_*` com `{"code":"TASK_TIMEOUT","category":"internal","retryable":true,
>    "message":"O processamento demorou mais que o esperado. Tente de novo."}` gravado no bloco da fase.
> 4. Isolamento: toda rota de exame/tentativa filtra por `doctor=request.user` (404 para outro usuário).
> 5. Testes de API (`exams/test_api.py`, modo `inline`, lambdas mockadas no nível do boto3 ou de
>    `services.invoke_contract`): fluxo feliz até `completed`; com avisos; recusa; erro retryable + `retry`;
>    erro não retryable + `retry` → 409; `reject` → `user_rejected` e nova tentativa no mesmo exame volta o
>    exam para `created`; erro de interpretação + `retry`; duplo `read/` → 409; acesso de outro usuário → 404;
>    `TASK_TIMEOUT`; `config` com e sem staff/flag; `health` sem token; modo `sqs` de `dispatch` chamando
>    `send_message` na fila certa com o corpo certo.
>
> Critérios de aceite: `python manage.py test` verde; `python manage.py check` sem erros; um `curl` manual com
> `runserver` + SQLite percorrendo criar exame → criar tentativa (pode pular o PUT real) funciona.
>
> **Relatório a devolver:** tabela de rotas implementadas (método, rota, status codes); exemplo real de JSON do
> `GET attempt` em `awaiting_validation` e em `completed` (tirado de um teste); como a idempotência do
> `run_task` foi garantida; desvios do §0.1 (se houver, justifique); testes novos.

---

## B4 — Container, worker SQS, settings de produção e SPA (backend)

**Prompt:**

> Repo: `ecg-backend`, branch do card. Leia antes: plano §0 (arquitetura alvo: Django em **ECS Fargate ARM**,
> dois containers da mesma imagem — `web` com gunicorn e `worker` consumindo SQS —, banco RDS PostgreSQL,
> segredos do SSM injetados como env vars pelo ECS). O backend já tem a API V1 com tarefas assíncronas
> (`exams/tasks.py`: `dispatch` / `run_task`, modos `ECG_TASK_MODE=inline|sqs`; confira os nomes reais).
> Agora é preparar a imagem de produção. **Nada de AWS real nesta etapa** (só código, testes e `docker build`).
>
> 1. Worker: comando `exams/management/commands/ecg_worker.py`. Loop de long polling na fila
>    `ECG_TASK_QUEUE_URL` (`receive_message`, `WaitTimeSeconds=20`, `MaxNumberOfMessages=1`); para cada
>    mensagem `{"ecg_task","attempt_id"}` chama `run_task` e só então `delete_message` (`run_task` nunca levanta
>    exceção; corpo inválido → loga e apaga). Trata `SIGTERM`/`SIGINT`: termina a mensagem em andamento e sai
>    com código 0 (o ECS dá `stopTimeout` de 120 s). Fecha conexões velhas do banco entre mensagens
>    (`django.db.close_old_connections()`). Loga uma linha por mensagem com `attempt_id`, tarefa e duração.
> 2. Settings de produção (`config/settings.py`):
>    - Banco: se `DATABASE_URL` existir, usa; senão monta a partir de `DB_HOST`, `DB_PORT` (5432), `DB_NAME`,
>      `DB_USER`, `DB_PASSWORD` com `sslmode=require`. `conn_max_age=60`, `conn_health_checks=True`. Se não for
>      SQLite e `DEBUG=False`, exigir `sslmode` em (`require`, `verify-ca`, `verify-full`), senão
>      `ImproperlyConfigured`.
>    - `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` por env (vão conter o host `*.execute-api.sa-east-1.amazonaws.com`
>      exato e `localhost` para o health check do container); `SECURE_PROXY_SSL_HEADER =
>      ("HTTP_X_FORWARDED_PROTO", "https")`, `SECURE_SSL_REDIRECT=False` (o API Gateway já é HTTPS), cookies
>      `Secure`, `APP_VERSION` por env.
>    - WhiteNoise: middleware logo após `SecurityMiddleware`, `STATIC_ROOT=BASE_DIR/"staticfiles"`, storage
>      comprimido com manifest.
>    - **SPA**: o export web do Expo fica em `BASE_DIR/"frontend_dist"` (gerado no build, fora do git: adicionar
>      ao `.gitignore`). Servir com `WHITENOISE_ROOT=frontend_dist` e uma view catch-all no fim de
>      `config/urls.py` que devolve `frontend_dist/index.html` para qualquer GET que não comece com `api/`,
>      `admin/` ou `static/` (se o arquivo não existir, 404 com mensagem clara).
>    - Logging: tudo para stdout, uma linha por evento, com `attempt_id` nos logs de `tasks`/`services`/worker.
> 3. Comando `accounts/management/commands/ensure_admin.py`: cria (se não existir) um superusuário staff com
>    `ADMIN_USERNAME`/`ADMIN_PASSWORD` do ambiente; idempotente; nunca imprime a senha.
> 4. `Dockerfile` (substituir o atual): base `public.ecr.aws/docker/library/python:3.12-slim` (ECR Public, evita o
>    limite do Docker Hub no CodeBuild), multi-stage se reduzir tamanho, usuário não-root, `pip install
>    --no-cache-dir`, copia o código (sem `tests/`, `mock_lambdas/`, `infra*/`, `docs/` — usar `.dockerignore`),
>    copia `frontend_dist/` se existir, roda `collectstatic --noinput` no build (com `DJANGO_SECRET_KEY` fictícia
>    só no build). `ENV PYTHONUNBUFFERED=1`. `CMD` padrão = web:
>    `gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60
>    --access-logfile -` (o worker usa o mesmo image com `command` sobrescrito na task definition).
>    `HEALTHCHECK` não vai no Dockerfile (fica na task definition); mas a imagem precisa conseguir rodar
>    `python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/api/health/', timeout=3)"`.
> 5. `requirements.txt`: adicionar `whitenoise`; remover `requests` se só o `smoke_e2e.py` usa (ver C1).
> 6. Testes (SQLite, boto3 mockado com `botocore.stub.Stubber` ou `unittest.mock`): worker processa uma mensagem
>    válida (chama `run_task` e apaga), corpo inválido é apagado sem chamar `run_task`, `SIGTERM` encerra o
>    loop após a mensagem atual; settings montam o banco a partir de `DB_*`; `GET /qualquer/rota` → `index.html`
>    quando `frontend_dist/index.html` existe (crie um temporário no teste) e `GET /api/inexistente/` continua
>    404 JSON; `ensure_admin` idempotente.
>
> Critérios de aceite: `python manage.py test` verde; `DEBUG=False DJANGO_SECRET_KEY=x… ALLOWED_HOSTS=example.com
> DATABASE_URL=postgres://u:p@h/db?sslmode=require python manage.py check --deploy` sem erros (warnings
> explicados); `docker build --platform linux/arm64 -t ecg-backend:local .` funciona (informe o tamanho); a
> imagem sobe com `docker run` + SQLite e `/api/health/` responde 200.
>
> **Relatório a devolver:** tamanho da imagem; lista das env vars que a task vai precisar (separando segredos
> do SSM e não segredos); saída do `check --deploy`; o trecho do worker que garante "apaga só depois de
> processar" e o tratamento de `SIGTERM`; como o catch-all evita engolir `/api/` 404 (cole o trecho); pontos
> incertos.


---

## F1 — Cliente da API e tipos no `packages/shared` (frontend)

**Prompt:**

> Repo: `leitor-ecg` (branch `feat/expo-v1-flow`, criar a partir de `main`). Leia antes, no repo
> `ecg-backend` (branch do card): `docs/plano-migracao-v1.md` §0.1 (contrato da API do backend — **fonte da
> verdade**), `docs/fluxo-usuario-v1.md` §3–4, `CONTRATO_LAMBDAS.md` §3.2 (formato de `summary`/`details`).
> Leia também `apps/mobile/AGENTS.md` e o `createApiClient` atual em `packages/shared`.
>
> 1. Em `packages/shared`, criar os tipos TypeScript do §0.1 (`AttemptStatus`, `Attempt`, `ReadingBlock`,
>    `InterpretationBlock`, `InterpretationSummary`, `InterpretationDetails`, `Exam`, `Patient`, `AppConfig`,
>    `Layout`, `TestScenario`, `ContractError`, `ContractWarning`).
> 2. Estender o `createApiClient` (mantendo `baseUrl` e `getToken`) com: `login`, `getConfig`, `listExams`,
>    `getExam`, `createExam`, `createAttempt`, `uploadImage(uploadUrl, headers, fileUri|Blob)` (PUT direto no
>    S3, mesmo `Content-Type` assinado; funcionar em web e native), `startRead`, `getAttempt`, `accept`,
>    `reject`, `retry`, `sendFeedback`, e `pollAttempt(examId, attemptId, {intervalMs=2000, timeoutMs=300000,
>    signal})` que resolve quando o status sai de `*_processando`.
> 3. Erros normalizados: `ApiError {kind: "network"|"unauthorized"|"validation"|"conflict"|"server", status,
>    message (pt-BR pronta para UI), fieldErrors?}`. 401 → `unauthorized` (o app vai deslogar).
> 4. `baseUrl` vazio = mesma origem (web servido pelo Django); no native vem de `EXPO_PUBLIC_API_BASE_URL`.
> 5. Remover do `shared` o que só servia ao fluxo antigo (`process/`, `aprovar/`, `rejeitar/`, `refine/`), se existir,
>    sem quebrar `apps/web` além do necessário (ele será aposentado; se quebrar a compilação dele, avise).
> 6. Testes: se o repo não tiver runner, adicionar `vitest` só no `packages/shared` e testar `pollAttempt`
>    (fetch mockado, intervalo, timeout, abort) e a normalização de erros.
>
> Critérios de aceite: `npx tsc --noEmit` limpo em `packages/shared` e `apps/mobile`; testes do shared verdes.
>
> **Relatório a devolver:** assinaturas exportadas; como `uploadImage` lida com web × native; o que foi
> removido; resultado do `tsc` e dos testes; divergências encontradas entre o §0.1 e o que o backend
> realmente devolve (se você conseguiu checar).

---

## F2 — Telas: login → exame → foto → layout → processando (frontend)

**Prompt:**

> Repo: `leitor-ecg`, branch `feat/expo-v1-flow`, app `apps/mobile` (Expo SDK 57 — consulte
> `https://docs.expo.dev/versions/v57.0.0/` antes de usar qualquer API do Expo). Leia antes, no `ecg-backend`:
> `docs/fluxo-usuario-v1.md` (seções 1, 3, 4) e `docs/plano-migracao-v1.md` §0.1. O `packages/shared` já tem
> o cliente da API e os tipos (etapa F1: `login`, `getConfig`, `createExam`, `createAttempt`, `uploadImage`,
> `startRead`, `pollAttempt`…; confira os nomes reais). Hoje o app só tem `LoginScreen` e `UploadScreen`, com
> problemas. Reaproveite os componentes do `shared` (`Button`, `Input`, `ChipSelect`, `Header`…) quando servirem.
>
> 1. Navegação (`src/navigation/RootNavigator.tsx`): pilha de auth × pilha do app, decidida pelo token salvo;
>    401 em qualquer chamada → limpa token e volta ao login.
> 2. Telas: `LoginScreen` (corrigir; erro legível; loading), `ExamsScreen` (lista de exames com status e
>    "Novo exame"; pull-to-refresh), `NewExamScreen` (nome, idade, sexo F/M obrigatório em controle segmentado;
>    validação inline), `CaptureScreen` (câmera ou galeria via `expo-image-picker`, prévia, velocidade e ganho
>    com os valores de `getConfig`, padrão 25/10), `LayoutScreen` (6 layouts com um mini-diagrama da grade de
>    cada um, segundo a tabela 2.2 do contrato), `ProcessingScreen` (cria a tentativa, faz o upload, chama
>    `startRead`, faz polling com mensagem de progresso e botão cancelar; ao terminar, navega conforme o estado —
>    as telas de destino são da F3, por enquanto use um placeholder que mostre o JSON do estado).
> 3. Seletor de **cenário de teste** (D7): só aparece na `CaptureScreen` quando `config.test_scenarios_enabled`
>    é `true`. Permite escolher cenário + código da leitura e da interpretação (valores da seção 4 do contrato);
>    vai no `createAttempt` como `test_scenario`. Visualmente marcado como "Modo de teste".
> 4. Acessibilidade e UX: `accessibilityLabel` em controles, alvo de toque ≥ 44 pt, textos escaláveis, estados de
>    loading/erro/vazio em toda tela, sem layout quebrado em web (largura máx. ~480 px centralizada) e em telas
>    pequenas.
> 5. Configuração: `EXPO_PUBLIC_API_BASE_URL` em `.env.example`; no web, se vazio, usar a mesma origem.
>
> Critérios de aceite: `npx tsc --noEmit` limpo; `npx expo start --web` abre e navega login → exames → novo
> exame → foto → layout → processando contra um backend local (`ecg-backend`: `python manage.py runserver` com
> `ECG_TASK_MODE=inline` e as lambdas mock reais da AWS, se houver credencial; senão contra um backend com
> `services.invoke_contract` stubado — diga qual usou). Capturas de tela das telas no relatório.
>
> **Relatório a devolver:** lista de telas e rotas de navegação; capturas de tela (web) de cada tela; como o
> polling é cancelado ao sair da tela; problemas encontrados no backend (com o JSON recebido) para eu validar;
> decisões de UI tomadas sem instrução.

---

## F3 — Telas: validação visual, recusa, erro, resultado e histórico (frontend)

**Prompt:**

> Repo: `leitor-ecg`, branch `feat/expo-v1-flow`, `apps/mobile` (Expo SDK 57; consultar a doc versionada).
> Leia antes, no `ecg-backend`: `docs/fluxo-usuario-v1.md` §3 e **§4 (tabela de desfechos — fonte da verdade
> desta etapa)**, `docs/plano-migracao-v1.md` §0.1 e `CONTRATO_LAMBDAS.md` §2.3, §2.4, §3.2. A F2 já entregou
> navegação, login, exames, novo exame, captura, layout e `ProcessingScreen` (que navega com um placeholder);
> confira o código real. Agora as telas de desfecho:
>
> 1. `ValidationScreen` (`awaiting_validation`): imagem `reading.overlay_url` (gridline + traço) com zoom de
>    pelo menos 3× e arraste nas três plataformas (iOS, Android, web); avisos (`reading.warnings`) em destaque
>    acima dos botões; qualidade por derivação (`reading.leads`, chips good/partial/saturated/low); botões
>    **Aceitar leitura** (→ `accept` → `ProcessingScreen` de interpretação) e **Recusar** (confirmação → `reject`
>    → oferece nova foto no mesmo exame).
> 2. `RefusedScreen` (`reading_refused`): `error.message`, prévia `refusal_preview_url` (destacar
>    `refusal_region_px` se a prévia não vier), dicas por código (`READ_REFUSED_BLURRY` → "apoie o celular…",
>    etc., para os 6 códigos), botão **Tirar nova foto**.
> 3. `ErrorScreen` (`reading_error` / `interpretation_error` e falhas de rede): mensagem específica; **Tentar de
>    novo** (`retry`) se `can_retry`, senão **Tirar nova foto** (`needs_new_photo`). Erro de rede → reconsulta
>    a tentativa (não perde estado). **Fora do horário** (o backend fica desligado seg–sex 20h–08h e fim de
>    semana; o API Gateway devolve 503 sem corpo JSON do Django): qualquer 503 cujo corpo não seja JSON do
>    backend → aviso "Serviço disponível de segunda a sexta, das 8h às 20h" (texto em constante única), sem
>    botão de retry automático em loop. Inclui a tela de login.
> 4. `ResultScreen` (`completed`): resumo em destaque (cor/ícone por `classification`, `headline`, ritmo, FC,
>    `key_findings`, `text`); avisos da interpretação e da leitura no topo; detalhado expansível: medidas
>    (tabela com valor, unidade e confiança; `null` = "não medível"), achados (com severidade), critérios (badge
>    "depende do sexo" quando `sex_specific`; status normal/abnormal/not_evaluated), por derivação; `disclaimer`
>    sempre visível; botão "Enviar feedback".
> 5. `ExamDetailScreen`: histórico de tentativas do exame com status; tocar numa tentativa abre a tela do estado
>    dela (reabrir o app no meio do fluxo precisa levar à tela certa).
> 6. A cor nunca pode ser o único indicador (texto/ícone junto). Textos todos em pt-BR.
>
> Critérios de aceite: `npx tsc --noEmit` limpo; contra o backend local com cenários de teste ligados
> (`ECG_TEST_SCENARIOS_ENABLED=true`, usuário staff), percorrer na web: leitura `success`,
> `success_with_warnings`, `refused` (pelo menos `READ_REFUSED_BLURRY` e `READ_REFUSED_OCCLUDED`), `failure` com
> `INTERNAL_ERROR` (deve aparecer "Tentar de novo") e com `READ_TRACE_EXTRACTION_FAILED` (deve aparecer "Tirar
> nova foto"), recusa pelo usuário, interpretação `success` com F e com M (referência do QTc muda),
> `success_with_warnings` e `failure`.
>
> **Relatório a devolver:** captura de tela de cada caso acima; como o zoom foi implementado em cada plataforma;
> lista de cenários que você não conseguiu reproduzir e por quê; inconsistências do JSON do backend frente ao §0.1.

---

## F4 — Export web servido pelo Django + verificação local ponta a ponta

**Prompt:**

> Repos: `leitor-ecg` (branch `feat/expo-v1-flow`) e `ecg-backend` (branch do card). Leia antes:
> `docs/plano-migracao-v1.md` §0 (arquitetura: o export web do Expo é servido pelo próprio Django (WhiteNoise), mesma
> origem da API). O backend já serve `frontend_dist/` via WhiteNoise + catch-all para `index.html` (etapa B4)
> e o app tem o fluxo completo (F2/F3).
>
> 1. `apps/mobile/app.json`: `web.output = "single"` (SPA). Garantir que rotas/links funcionam a partir de `/`
>    e que os assets saem com caminho absoluto `/…` compatível com o WhiteNoise.
> 2. Script `ecg-backend/scripts/build_frontend.sh <caminho-do-leitor-ecg>`: `npm ci` na raiz do monorepo,
>    `npx expo export --platform web` em `apps/mobile` com `EXPO_PUBLIC_API_BASE_URL=` (vazio = mesma origem),
>    copia `dist/` para `ecg-backend/frontend_dist/`.
> 3. Verificação local: `build_frontend.sh` + `python manage.py collectstatic` + `runserver` (ou a imagem
>    Docker da B4 com `docker run`) e percorrer na web, **servida pelo Django**, os mesmos casos da F3. Conferir que
>    `/api/...` inexistente continua devolvendo 404 JSON e que recarregar a página numa rota interna do app
>    devolve o app.
> 4. Rodar o app no **Expo Go** (celular na mesma rede, `EXPO_PUBLIC_API_BASE_URL=http://<ip-da-máquina>:8000`,
>    `ALLOWED_HOSTS` incluindo o IP) e percorrer pelo menos: sucesso completo, recusa e erro retryable.
> 5. Atualizar `README.md` do backend (seções de execução local e build) — curto.
>
> Critérios de aceite: casos da F3 passando na web servida pelo Django; os 3 casos no Expo Go.
>
> **Relatório a devolver:** tamanho do `frontend_dist`; capturas (web servida pelo Django + Expo Go);
> problemas de caminho de asset/roteamento encontrados e a correção; qualquer coisa que funcione em
> `expo start --web` e não no export.

---

## I1 — Terraform: rede, RDS, ECR, SQS e IAM (somente `plan`)

**Prompt:**

> Repo: `ecg-backend`, branch do card. Leia antes: `docs/plano-migracao-v1.md` §0 (arquitetura alvo, lista do que
> é proibido) e §0.2 (custo), e as env vars que o backend precisa (`config/settings.py`, `Dockerfile`,
> `exams/management/commands/ecg_worker.py`, `README.md`). Referência de estilo: `mock_lambdas/infra/`.
> **Não rode `terraform apply`.** Antes de usar argumentos write-only, confira na documentação do provider que
> a versão escolhida os suporta.
>
> Criar `infra/aws/` (Terraform ≥ 1.11, provider aws na versão estável mais recente que suporte `password_wo`
> em `aws_db_instance` e o ephemeral `aws_ssm_parameter`), backend S3 `ecg-lambdas-terraform-state`, key
> `ecg-backend-prod/terraform.tfstate`, região `sa-east-1`, `default_tags` `project=ecg`, `env=prod`.
> Arquivos separados por assunto (`network.tf`, `rds.tf`, `ecr.tf`, `sqs.tf`, `iam.tf`, `variables.tf`, `outputs.tf`).
> 1. Rede: VPC `10.20.0.0/16` com DNS habilitado; 2 subnets públicas (2 AZs) com IGW; 2 subnets privadas (2 AZs)
>    com route table **sem** rota para a internet. Sem NAT, sem endpoints de VPC. Security groups:
>    `ecg-vpclink` (sem ingress; egress 8000 para `ecg-ecs`), `ecg-ecs` (ingress 8000 só do `ecg-vpclink`;
>    egress 443 para 0.0.0.0/0 e 5432 para `ecg-rds`), `ecg-rds` (ingress 5432 só do `ecg-ecs`).
> 2. RDS PostgreSQL (última versão 17.x disponível): `db.t4g.micro`, Single-AZ, gp3 20 GB com
>    `max_allocated_storage = 50`, criptografado (chave AWS gerenciada), subnet group nas privadas,
>    `publicly_accessible = false`, `backup_retention_period = 7`, janela de backup 03:00–04:00 UTC **não**
>    serve (o banco fica desligado de madrugada): usar janela dentro do horário ligado, ex. `14:00-14:30` UTC, e
>    manutenção `wed:15:00-wed:15:30` UTC; `deletion_protection = true`, `skip_final_snapshot = false`,
>    `copy_tags_to_snapshot = true`, `auto_minor_version_upgrade = true`, Performance Insights/Database Insights
>    no nível gratuito (7 dias), parameter group com `rds.force_ssl = 1` e `log_min_duration_statement = 1000`.
>    Usuário mestre `ecg_admin`; senha **fora do state**: lida do SSM `/ecg/prod/DB_PASSWORD` (criado à mão pelo
>    Gus) com `ephemeral "aws_ssm_parameter"` e passada em `password_wo` + `password_wo_version` (variável).
>    Banco inicial `ecg`.
> 3. ECR `ecg-backend`: `image_tag_mutability = IMMUTABLE`, scan on push, lifecycle mantendo as 10 imagens mais
>    recentes.
> 4. SQS `ecg-tasks` (SSE-SQS, `visibility_timeout_seconds = 900`, `receive_wait_time_seconds = 20`,
>    `message_retention_seconds = 86400`) com DLQ `ecg-tasks-dlq` (`maxReceiveCount = 3`, retenção 14 dias).
> 5. IAM (mínimo privilégio, só as roles; quem usa é a I2):
>    - `ecg-ecs-execution`: `AmazonECSTaskExecutionRolePolicy` + `ssm:GetParameters` em
>      `arn:…:parameter/ecg/prod/*` + `kms:Decrypt` em `alias/aws/ssm` com `kms:ViaService =
>      ssm.sa-east-1.amazonaws.com`.
>    - `ecg-ecs-task`: `s3:PutObject` no bucket de imagens `ecg-lambdas-images-785236377332-sa-east-1`
>      (prefixo `*/original.*`, para assinar o PUT); `s3:GetObject` nesse bucket e em
>      `ecg-lambdas-results-785236377332-sa-east-1`; `lambda:InvokeFunction` só nas duas lambdas do contrato
>      (nomes por variável, padrão `ecg-reading-mock` / `ecg-interpretation-mock`); `sqs:SendMessage`,
>      `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes`, `ChangeMessageVisibility` só na `ecg-tasks`.
> 6. Outputs: `vpc_id`, ids das subnets públicas/privadas, ids dos 3 SGs, `db_endpoint`, `db_name`, `db_username`,
>    `ecr_repository_url`, `queue_url`, `queue_arn`, `dlq_arn`, ARNs das 2 roles.
> 7. `infra/aws/README.md` curto: pré-requisitos, parâmetros SSM a criar à mão (nomes e tipo `SecureString`, sem
>    valores: `DB_PASSWORD`, `DJANGO_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`), comandos `init/plan/apply`.
>
> Critérios de aceite: `terraform fmt -check`, `terraform validate` e `terraform plan` sem erros (o plan precisa
> do parâmetro `/ecg/prod/DB_PASSWORD` existir; se não existir, rode o plan com `-target` excluindo o RDS e diga
> isso no relatório). Nada aplicado.
>
> **Relatório a devolver:** recursos do `plan` (contagem por tipo); as policies das 2 roles (JSON); prova de que
> a senha do banco não aparece no state/plan (trecho do plan mostrando `password_wo` oculto); versão do
> PostgreSQL e do provider escolhidas; recursos que custam dinheiro parado (esperado: só o disco do RDS e o ECR);
> pontos incertos.

---

## I2 — Terraform: ECS Fargate, API Gateway, horário comercial e monitoramento (somente `plan`)

**Prompt:**

> Repo: `ecg-backend`, branch do card. Leia antes: `docs/plano-migracao-v1.md` §0 e §0.2, e `infra/aws/` (etapa
> I1: VPC, subnets, SGs `ecg-vpclink`/`ecg-ecs`/`ecg-rds`, RDS, ECR, SQS, roles `ecg-ecs-execution`/`ecg-ecs-task`
> — **confira os nomes reais dos recursos e outputs no código**). Também o `Dockerfile` e o comando
> `ecg_worker`. **Não rode `terraform apply`.** Mesmo state e pasta da I1, arquivos novos (`ecs.tf`, `api.tf`,
> `schedule.tf`, `monitoring.tf`).
>
> 1. ECS: cluster `ecg` (Container Insights **desligado**). Task definition `ecg-backend`: Fargate, `ARM64`,
>    `cpu = var.task_cpu` (padrão 512), `memory = var.task_memory` (padrão 1024), `awsvpc`, roles da I1, imagem
>    `<ecr>:${var.image_tag}` (padrão `bootstrap`). Dois containers:
>    - `web` (essential, porta 8000, health check com o `python -c urllib…/api/health/` do B4, interval 30,
>      startPeriod 60);
>    - `worker` (essential, `command = ["python","manage.py","ecg_worker"]`, `stopTimeout = 120`).
>    Ambos com log driver `awslogs` no log group `/ecs/ecg-backend` (14 dias), env vars não secretas
>    (`DEBUG=False`, `ECG_TASK_MODE=sqs`, `ECG_TASK_QUEUE_URL`, `DB_HOST`, `DB_NAME`, `DB_USER`,
>    `AWS_IMAGES_BUCKET`, `AWS_RESULTS_BUCKET`, `AWS_LAMBDA_ECG_READING`, `AWS_LAMBDA_ECG_INTERPRETATION`,
>    `ECG_TEST_SCENARIOS_ENABLED` = variável padrão `true`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `APP_VERSION`)
>    e `secrets` vindos do SSM (`DB_PASSWORD`, `DJANGO_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`).
> 2. Serviço `ecg-backend`: Fargate (`platform_version = "LATEST"`), subnets **públicas** com
>    `assign_public_ip = true`, SG `ecg-ecs`, `deployment_circuit_breaker { enable = true, rollback = true }`,
>    min healthy 100 % / max 200 %, `enable_execute_command = false`, `service_registries` apontando para um
>    serviço Cloud Map (namespace DNS privado `ecg.internal` na VPC, registro **SRV**, `container_name = "web"`,
>    `container_port = 8000`). `lifecycle { ignore_changes = [desired_count, task_definition] }` (o pipeline faz
>    o deploy e o Scheduler/autoscaling controla a quantidade).
> 3. Autoscaling (Application Auto Scaling): alvo `min 1 / max 2`, target tracking CPU 70 %. **Ações agendadas**
>    no fuso `America/Sao_Paulo`: `cron(0 8 ? * MON-FRI *)` → min 1/max 2; `cron(0 20 ? * MON-FRI *)` → min 0/max 0.
>    Horários em variáveis.
> 4. RDS ligado/desligado: EventBridge Scheduler (grupo `ecg`, fuso `America/Sao_Paulo`) com alvos universais
>    `arn:aws:scheduler:::aws-sdk:rds:startDBInstance` (`cron(40 7 ? * MON-FRI *)`) e `…:stopDBInstance`
>    (`cron(10 20 ? * MON-FRI *)`), role própria só com `rds:StartDBInstance`/`StopDBInstance` na instância.
>    Variável `office_hours_enabled` (padrão `true`): se `false`, nenhuma ação agendada é criada e o mínimo fica 1.
> 5. API Gateway **HTTP API**: VPC Link (subnets públicas, SG `ecg-vpclink`); integração `HTTP_PROXY` privada
>    com `integration_uri` = ARN do serviço Cloud Map, `connection_type = VPC_LINK`, timeout 30 s; rota
>    `$default`; stage `$default` com auto-deploy, throttling (burst 50, rate 20) e access log JSON num log group
>    próprio (14 dias).
> 6. Scripts `scripts/ligar.sh` e `scripts/desligar.sh` (`set -euo pipefail`, usam a AWS CLI): `ligar` inicia o
>    RDS (se parado), espera `db-instance-available`, coloca o alvo de autoscaling em min 1/max 2 e espera o
>    serviço estabilizar; `desligar` faz o inverso. Para uso fora do horário.
> 7. Monitoramento: tópico SNS `ecg-alerts` com assinatura de e-mail (variável `alarm_email`). Exatamente 10
>    alarmes (free tier), todos com `treat_missing_data = notBreaching` e ação no SNS: 5xx do API Gateway ≥ 5 em
>    5 min; CPU do serviço ECS > 80 % por 10 min; memória do serviço ECS > 85 % por 10 min; CPU do RDS > 80 % por
>    10 min; `FreeStorageSpace` < 2 GB; `CPUCreditBalance` < 20; DLQ `ApproximateNumberOfMessagesVisible` ≥ 1;
>    `ApproximateAgeOfOldestMessage` da fila > 300 s; `Errors` de cada lambda do contrato ≥ 1 em 5 min.
> 8. Custo: `aws_budgets_budget` mensal de US$35 com aviso por e-mail em 80 % do real e 100 % da previsão.
> 9. Outputs: `api_url`, `cluster_name`, `service_name`, `task_family`, `log_group`, `sns_topic_arn`.
> 10. CORS do bucket de imagens: **não gerenciar aqui** (é do repo `ecg-lambdas`,
>     `infra/terraform/modules/shared`). Escreva no relatório a mudança exata (adicionar a origem
>     `https://<api-id>.execute-api.sa-east-1.amazonaws.com` em `AllowedOrigins` do PUT).
> 11. Atualizar `infra/aws/README.md`: horário de funcionamento, `ligar.sh`/`desligar.sh`, como mudar
>     `task_cpu`.
>
> Critérios de aceite: `terraform fmt -check`, `validate` e `plan` (com `-var alarm_email=...`) sem erros;
> scripts passam no `shellcheck` (se disponível). Nada aplicado.
>
> **Relatório a devolver:** recursos novos do `plan`; a task definition renderizada (JSON, sem segredos); os
> 10 alarmes (nome, métrica, limiar); as ações agendadas com fuso; a mudança de CORS a fazer no `ecg-lambdas`;
> pontos incertos — em especial: o `Host` que chega ao Django via VPC Link (o `ALLOWED_HOSTS` depende disso) e se
> o registro SRV + health do container funcionam com a integração Cloud Map do HTTP API.

---

## I3 — CI/CD na AWS: CodePipeline V2 + CodeBuild (somente `plan`)

**Prompt:**

> Repo: `ecg-backend`, branch do card. Leia antes: `docs/plano-migracao-v1.md` §0, `infra/aws/` (I1 e I2: ECR
> `ecg-backend`, cluster `ecg`, serviço `ecg-backend`, task definition `ecg-backend` com containers `web` e
> `worker`, SNS `ecg-alerts`, scripts `ligar.sh`; **confira os nomes reais**), `Dockerfile` e
> `scripts/build_frontend.sh`. **Não rode `terraform apply`.**
>
> 1. Buildspecs em `ci/`:
>    - `buildspec-test.yml` (Python 3.12): instala `requirements.txt`, roda `python manage.py makemigrations
>      --check --dry-run`, `python manage.py test`, `python -m unittest discover -s mock_lambdas/tests`.
>    - `buildspec-build.yml` (privileged, Node 22 + Docker): fonte secundária `leitor-ecg` em
>      `$CODEBUILD_SRC_DIR_frontend`; `scripts/build_frontend.sh "$CODEBUILD_SRC_DIR_frontend"`; `APP_VERSION` =
>      `<sha curto backend>-<sha curto front>`; `docker build --build-arg APP_VERSION` e push para o ECR com a tag
>      `APP_VERSION` (tags imutáveis: se a tag já existir, pular o push). Artefato de saída: `image.env`
>      (`IMAGE_TAG=…`) + `ci/deploy.sh`.
>    - `buildspec-deploy.yml` chamando `ci/deploy.sh`.
> 2. `ci/deploy.sh` (`set -euo pipefail`):
>    1. Se o RDS estiver parado ou o alvo de autoscaling estiver em max 0 (fora do horário), roda
>       `scripts/ligar.sh` e avisa no log que o ambiente fica ligado até o próximo desligamento agendado.
>    2. Lê a task definition atual do serviço, troca a imagem dos containers `web` e `worker` para a nova tag e
>       `APP_VERSION`, registra uma nova revisão.
>    3. Migração: `aws ecs run-task` com a nova revisão, mesmas subnets/SG/IP público do serviço, `command`
>       sobrescrito no container `web` para `python manage.py migrate --noinput` e `worker` para
>       `python -c "pass"`; espera `tasks-stopped` e falha se o exit code do `web` ≠ 0 (imprime o fim do log).
>    4. `ensure_admin` do mesmo jeito (idempotente).
>    5. `aws ecs update-service` com a nova revisão e `aws ecs wait services-stable` (o circuit breaker volta
>       sozinho se as tasks não ficarem saudáveis).
>    6. Smoke: `curl` em `<api_url>/api/health/` (espera 200 e `version` igual à nova) e em `<api_url>/`
>       (espera HTML). Se falhar, `update-service` para a revisão anterior, espera estabilizar e sai com erro.
> 3. Terraform em `infra/aws/cicd.tf` (mesmo state):
>    - `aws_codestarconnections_connection` GitHub (fica PENDENTE até o Gus autorizar no console — documente).
>    - 3 projetos CodeBuild (`ecg-backend-test`, `-build`, `-deploy`): ARM
>      (`aws/codebuild/amazonlinux-aarch64-standard:3.0`, `BUILD_GENERAL1_SMALL`; o `build` com
>      `privileged_mode = true`), logs com retenção de 14 dias, timeout 20 min, roles separadas e mínimas (o
>      `build` só faz push no ECR `ecg-backend`; o `deploy` só mexe no serviço/task definition do ECS, `run-task`
>      com `iam:PassRole` restrito às 2 roles da task, `rds:StartDBInstance`/`DescribeDBInstances` na instância,
>      `application-autoscaling` no alvo do serviço, leitura dos logs da migração).
>    - Bucket de artefatos do pipeline `ecg-backend-pipeline-<account_id>` (bloqueio público, SSE-S3, lifecycle
>      apagando objetos após 30 dias).
>    - CodePipeline **V2**, modo `QUEUED`. Stages: Source (duas ações: `tl-gmiler/ecg-backend` e
>      `trustedlake/leitor-ecg` (conexão própria, `ecg-github-frontend`), branch `main`, artefatos `backend` e `frontend`; gatilho em push na `main` de
>      qualquer um) → Test → Build → Deploy.
>    - Notificação de falha do pipeline no SNS `ecg-alerts` (regra do EventBridge).
> 4. Remover `.github/workflows/docker-publish.yml` (o CI/CD passa a ser na AWS) — mencione no relatório.
> 5. Atualizar `infra/aws/README.md`: autorizar a conexão do GitHub, disparar o pipeline manualmente, o que
>    acontece num deploy fora do horário.
>
> Critérios de aceite: `terraform fmt -check`, `validate`, `plan` sem erros; `ci/deploy.sh` passa no `shellcheck`
> (se disponível).
>
> **Relatório a devolver:** recursos do `plan`; as policies das 3 roles do CodeBuild e da do pipeline (JSON); o
> `ci/deploy.sh` completo; estimativa de minutos por execução (free tier: 100 min/mês do CodeBuild e 100 min de
> ação do CodePipeline V2) e custo; pontos incertos (ex.: nome da variável da fonte secundária, tempo do
> `docker build` ARM).

---


## B6 — Leitura no contrato 2.0 (`ecg-reading`) — feita em 19/10

Motivo: entre 15 e 19/10, os PRs #6, #7 e #8 do backend e o PR #2 do `ecg-lambdas-v1` levaram as mocks e o
contrato para o `ecg-lambdas-v1`, ligaram o backend antigo à `ecg-reading` real (contrato 2.0) e apagaram as
lambdas `-mock`. O que mudou na V1:

- Merge da `origin/main`, mantendo a reescrita da V1 e portando a lógica do Chris.
- `exams/services.py`:
  - fica com o método `OK` de maior `selector_score`;
  - overlay = `files.image` do método e `signal` da interpretação = `files.leads` (`leads.json`), com
    `sampling_rate_hz` do método e `units` mV;
  - avisos vêm do método escolhido; `METHODS_PARTIAL_RESULT` entra quando a lambda diz `OK_WITH_WARNINGS`
    sem nenhum aviso;
  - na recusa, a prévia é a imagem de recusa do primeiro método;
  - `reading.method` / `reading.methods` guardam a escolha (auditoria, fora da API);
  - `leads.json` ilegível vira `STORAGE_ERROR` retryable.
- Layouts aceitos: só `3x4+1` e `12x1` (os demais dão `LAYOUT_UNSUPPORTED` no 2.0). O cenário de teste da
  leitura não leva mais `code`.
- Tempos: boto `read_timeout` 930 s, `STALE_PROCESSING_SECONDS` 1200 s, visibility da `ecg-tasks` 960 s,
  alarme de idade da fila 600 s, polling do app até 20 min. O worker roda `ECG_WORKER_CONCURRENCY` (4)
  threads, para uma leitura de 50 a 190 s não segurar a fila. Uma leitura cortada pelo `stopTimeout`
  (120 s) num deploy volta para a fila depois da visibility e é refeita.
- Backend local: `scripts/local_server.py`, com a leitura 2.0 falsa em `scripts/local_lambdas.py` e a
  interpretação mock do clone do `ecg-lambdas-v1` (`--lambdas-repo`).
- A confirmar no E1, contra a lambda de verdade: o código e o formato da recusa e do erro dos cenários
  `mock` (o fake local usa `READ_REFUSED_UNREADABLE_TRACE` / `READ_TRACE_EXTRACTION_FAILED`), onde vêm os
  avisos do `success_with_warnings` e o tempo de cold start da imagem de 3,5 GB.

O E1 abaixo foi escrito para o contrato v1: os casos de `refused`/`failure` por código passam a ser um caso
por cenário, e entra um caso de leitura real (sem `mock`) com uma foto de ECG de verdade.


## D1 — Primeiro deploy em prod (executar **junto com o Gus**, cada passo confirmado)

Checklist (não é prompt para agente rodar sozinho):

1. Gus: criar os parâmetros SSM `SecureString` em `sa-east-1`: `/ecg/prod/DB_PASSWORD` (gerar com
   `python -c "import secrets;print(secrets.token_urlsafe(32))"`), `/ecg/prod/DJANGO_SECRET_KEY` (idem, 50),
   `/ecg/prod/ADMIN_USERNAME`, `/ecg/prod/ADMIN_PASSWORD`.
2. `terraform -chdir=infra/aws apply` (revisar o plan antes; o RDS leva ~10 min) → anotar `api_url`. O serviço
   sobe com a imagem `bootstrap`, que ainda não existe: ele fica tentando até o primeiro deploy do pipeline
   (esperado; o circuit breaker não age antes do primeiro deploy).
3. Gus: autorizar as duas conexões do GitHub no console (CodePipeline → Settings → Connections): `ecg-github`
   na conta `tl-gmiler` (backend) e `ecg-github-frontend` na conta `trustedlake` (o `leitor-ecg` foi transferido
   para lá); e confirmar a assinatura do e-mail do SNS.
4. Aplicar a mudança de CORS do bucket de imagens no repo `ecg-lambdas` (relatório da I2) — só para o upload pela
   web; o Expo Go nativo não precisa de CORS.
5. Merge das branches na `main` dos dois repos (PRs revisados) → pipeline roda sozinho (migrate + `ensure_admin`).
   Acompanhar; conferir no log da task qual `Host` chega e ajustar `ALLOWED_HOSTS` se preciso.
6. Entrar no `/admin/` com o admin e criar o usuário de teste staff.
7. Conferir o horário: no dia seguinte, ver no console que o RDS parou 20:10 e o serviço foi a 0 às 20:00, e que
   voltaram às 07:40/08:00.
8. Expo Go: `EXPO_PUBLIC_API_BASE_URL=<api_url> npx expo start` (ou EAS Update, se decidirmos depois).


## E1 — Teste ponta a ponta em prod

**Prompt:**

> Repos `ecg-backend` (branch do card) e `leitor-ecg`. O sistema está em prod: API e web em `<api_url>`
> (fornecido pelo Gus), lambdas mock com cenários selecionáveis, usuário staff de teste (credenciais fornecidas
> pelo Gus fora do repo). Leia `docs/fluxo-usuario-v1.md` §4 e `CONTRATO_LAMBDAS.md` §4–5.
>
> 1. Script `scripts/e2e_prod.py` (substitui `smoke_e2e.py`): via API, com uma foto de teste válida (≥ 800×500 px)
>    em `tests/fixtures/`, percorre a matriz abaixo, verificando estado final, `can_retry`/`needs_new_photo`,
>    presença das URLs e que as URLs presigned respondem 200. Saída em tabela + código de saída ≠ 0 se algo
>    falhar. Credenciais por env (`E2E_USERNAME`, `E2E_PASSWORD`, `E2E_API_URL`).
> 2. Matriz mínima:
>    - leitura `success` → aceitar → interpretação `success` (sexo F e sexo M; conferir referência do QTc)
>    - leitura `success_with_warnings` → aceitar → interpretação `success_with_warnings`
>    - leitura `refused` com cada um dos 6 códigos `READ_REFUSED_*`
>    - leitura `failure` com `READ_TRACE_EXTRACTION_FAILED`, `READ_GRID_NOT_FOUND` (needs_new_photo) e
>      `STORAGE_ERROR`, `INTERNAL_ERROR` (can_retry → `retry` → sem cenário de falha no retry deve concluir)
>    - leitura `success` → recusar → nova tentativa no mesmo exame
>    - interpretação `failure` com `INTERP_NO_QRS_DETECTED`, `INTERP_SIGNAL_TOO_NOISY`, `INTERNAL_ERROR`
>    - validação real: imagem pequena demais (`IMAGE_TOO_SMALL`) e arquivo que não é imagem
>      (`IMAGE_FORMAT_UNSUPPORTED`/`IMAGE_UNREADABLE`)
>    - segurança: rota de exame de outro usuário → 404; sem token → 401; `test_scenario` com usuário não staff → 400
> 3. Roteiro manual de UI (web no navegador **e** Expo Go Android/iOS) cobrindo pelo menos um caso de cada linha
>    da tabela §4 do fluxo; salvar capturas em `docs/evidencias/e2e-prod/` e a tabela de resultados em
>    `docs/evidencias/e2e-prod/RESULTADOS.md`.
> 4. Observabilidade: conferir que os logs da tentativa aparecem no CloudWatch com `attempt_id` e que um alarme
>    dispara (ex.: forçar `INTERNAL_ERROR` não basta — use `aws cloudwatch set-alarm-state` para testar o SNS).
>
> **Relatório a devolver:** a tabela de resultados (caso, esperado, obtido, evidência); bugs encontrados com
> passos para reproduzir e o JSON da resposta; tempos médios de leitura e interpretação (incluindo a primeira requisição do dia, logo após o serviço ligar).

## C1 — Limpeza do legado (com confirmação do Gus a cada item)

- Desabilitar e depois apagar as distribuições CloudFront do ECG: `E1919M81GCZ1D7` ("HTTPS proxy para
  ecg-backend"), `E35D6VSM7GCHQ8` ("SPA leitor-ecg (React)"), `E2DDW6M1G1CAEK` ("leitor-ecg-spa-dev"). **Perguntar**
  sobre `E1ZP3N2CBOTW7S` ("leitorecg - site institucional") — não é do app.
- Desligar a VM Oracle e remover `infra/terraform/` (EC2 + CloudFront), `infra-oracle/`, `infra/deploy.sh`,
  `smoke_e2e.py`, `INTEGRACAO_ECG.md` (o conteúdo útil vai para o README).
- Banco antigo (Neon): **sem migração de dados** (decidido em 2026-10-15) — o RDS de prod nasce limpo; o Neon é só abandonado.
- Aposentar `leitor-ecg/apps/web` e os buckets `leitor-ecg-spa-*` (confirmar antes de apagar dados).
- Registrar na KB (tags `ecg`, `aws`, `deploy`, `arquitetura`) a arquitetura final e o link do `infra/aws/README.md`
  — insumo dos cards de diagrama, instrução de deploy e relatório técnico.
