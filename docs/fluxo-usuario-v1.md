# Fluxo do usuário — V1 (revisão de 2026-10-15)

> Status: **APROVADO para implementação.** D1–D4 e D8 decididas com o Gus em 2026-10-15 (D3/D4/D6 revistas no
> mesmo dia: tudo na AWS, Django em ECS Fargate, banco RDS). O plano de execução está em [`plano-migracao-v1.md`](plano-migracao-v1.md).

## 1. Fluxo alvo (planning, KB "ECG — Planning")

1. Login
2. Novo exame: dados do paciente (nome, idade, **sexo F/M**, usado na interpretação)
3. Upload/foto do ECG em papel (JPG/PNG/WebP) + calibração (velocidade e ganho)
4. Escolha do layout de derivações (`3x4`, `3x4+1`, `3x4+3`, `6x2`, `6x2+1`, `12x1`)
5. **Leitura** (lambda de leitura): sinal + imagem original com traço extraído e gridline
   - Recusa da lambda (imagem borrada/cortada/sobreposta…) → mostra o motivo + prévia e pede nova foto
6. Validação visual pelo usuário → **aceitar** ou **recusar**
   - Recusar → nova tentativa (nova foto) no mesmo exame
7. **Interpretação** (lambda de interpretação): sinal + sexo → resumo geral + resultado detalhado
8. Tela de resultado: resumo em destaque + detalhado expansível + avisos + aviso de que não é laudo médico

## 2. Decisões

| # | Tema | Decisão | Status |
|---|---|---|---|
| D1 | Contrato | Contrato v1 (`CONTRATO_LAMBDAS.md`) com as mocks `ecg-reading-mock` / `ecg-interpretation-mock` já deployadas. Trocar mock por real = trocar o nome da função na env. | **decidida** |
| D2 | Refine (pincel) | Sai da V1. Recusar a leitura = nova foto. Código de `refine` e a integração com `ecg-analyser` são removidos do backend. | **decidida** |
| D3 | Hosting | **Tudo na AWS.** Django em **ECS Fargate ARM** (0,5 vCPU / 1 GB, 1–2 tasks) atrás de API Gateway HTTP API + VPC Link + Cloud Map (HTTPS nativo `execute-api`, sem ALB). App web (export do Expo) servido pelo próprio Django via WhiteNoise. Tasks em subnet pública com SG fechado (sem NAT). **Sem CloudFront.** Lambda para o Django foi vetada (preferência por boas práticas de container). | **decidida** |
| D4 | Banco | **RDS PostgreSQL `db.t4g.micro` Single-AZ**, gp3 20 GB, subnet privada, TLS obrigatório, backup 7 dias, proteção contra exclusão. Multi-AZ fora. Senha no SSM (fora do state do Terraform). | **decidida** |
| D5 | Conta/região | Conta `785236377332`, região `sa-east-1`. | assumida |
| D6 | Processamento | Assíncrono: o endpoint dispara a tarefa e responde 202, a tarefa vai para uma fila SQS (com DLQ) consumida por um container `worker` na mesma task do ECS, que chama a lambda de leitura/interpretação, e o app consulta o estado (polling a cada 2 s). Motivo: o API Gateway corta em 30 s, e os scripts reais podem demorar mais que as mocks. | decidida (técnica) |
| D8 | Horário de funcionamento | Seg–sex 08h–20h (America/Sao_Paulo). EventBridge Scheduler liga/desliga RDS e ECS; fora do horário a URL devolve 503 e o app mostra o horário. `scripts/ligar.sh` para exceções. Custo ≈ US$25/mês (24/7 seria ≈ US$60). | **decidida** |
| D7 | Cenários de teste em prod | Seleção de cenário mock (`success`, `success_with_warnings`, `failure`, `refused` + `code`) por tentativa, só para usuário `is_staff` e só com a flag `ECG_TEST_SCENARIOS_ENABLED=true`. Sem isso, o campo é rejeitado. | decidida (técnica) |

Fora de escopo da V1: fallback de IA (V2), domínio próprio (a URL `execute-api` já tem HTTPS; domínio
customizado no API Gateway pode entrar depois sem custo extra além do DNS), alta disponibilidade multi-AZ do banco.

## 3. Máquina de estados da tentativa (`ExamAttempt.status`)

```
criado ──read──▶ leitura_processando ──▶ aguardando_validacao ──accept──▶ interpretacao_processando ──▶ concluido
                        │                        │                                │
                        ├──▶ leitura_recusada    └──reject──▶ rejeitado_usuario   └──▶ erro_interpretacao
                        └──▶ erro_leitura                                              (retry se retryable)
                             (retry se retryable)
```

- Estados finais que pedem **nova foto** (nova tentativa no mesmo exame): `leitura_recusada`,
  `rejeitado_usuario`, `erro_leitura`/`erro_interpretacao` com `retryable=false`.
- `retry` só é aceito em `erro_leitura` / `erro_interpretacao` com `retryable=true` (`STORAGE_ERROR`,
  `INTERNAL_ERROR`, `TASK_TIMEOUT`). Reexecuta a fase que falhou na mesma tentativa.
- Tentativa presa em `*_processando` além de `STALE_PROCESSING_SECONDS` vira `erro_*` com código
  `TASK_TIMEOUT` (retryable).
- `Exam.status` espelha o status da tentativa mais recente.

## 4. O que a UI mostra em cada desfecho

| Fase | Status da lambda | Estado da tentativa | Tela / ação |
|---|---|---|---|
| Leitura | `OK` | `aguardando_validacao` | Validação visual: overlay com gridline + traço, zoom, botões **Aceitar** / **Recusar** |
| Leitura | `OK_WITH_WARNINGS` | `aguardando_validacao` | Igual, com a lista de avisos em destaque (ex.: "V2 parcial") acima dos botões |
| Leitura | `REFUSED` | `leitura_recusada` | Motivo (`error.message`) + prévia com a região destacada + **Tirar nova foto** |
| Leitura | `ERROR` | `erro_leitura` | Mensagem específica; **Tentar de novo** se `retryable`, senão **Tirar nova foto** |
| Interpretação | `OK` | `concluido` | Resultado: resumo (classificação, headline, ritmo, FC, achados) + detalhado expansível + disclaimer |
| Interpretação | `OK_WITH_WARNINGS` | `concluido` | Igual, com os avisos visíveis no topo do resultado |
| Interpretação | `ERROR` | `erro_interpretacao` | Mensagem específica; **Tentar de novo** se `retryable`, senão **Tirar nova foto** |
| Qualquer | falha de rede/backend (5xx, timeout) | — | Mensagem genérica + **Tentar de novo** (não perde o estado: o app reconsulta a tentativa) |
| Qualquer | fora do horário (503 do API Gateway, D8) | — | Aviso "Serviço disponível de segunda a sexta, das 8h às 20h" |

Regras de texto: a UI mostra `message` (pt-BR, vindo da lambda) e decide o fluxo por `code`/`retryable`,
nunca por texto. A tela de resultado sempre mostra o `disclaimer`.

## 5. Gaps do estado atual (2026-10-15)

| Área | Hoje | Alvo |
|---|---|---|
| Backend | 1 lambda `ecg-analyser` (`analyze` + `refine`), síncrono, layout fixo `3x4+1` | Contrato v1, 2 lambdas, assíncrono, 6 layouts, sexo F/M |
| App Expo (`leitor-ecg/apps/mobile`, SDK 57) | Só `LoginScreen` e `UploadScreen` | Fluxo completo da seção 1 + estados da seção 4 |
| Web legado (`leitor-ecg/apps/web`, Vite) | 14 páginas, parte estática, servido por CloudFront | Substituído pelo export web do app Expo |
| Hosting | Django em VM Oracle atrás de CloudFront (`E1919M81GCZ1D7`) | ECS Fargate + API Gateway (D3) |
| Banco | Neon | RDS PostgreSQL (D4) |
| Segredos | Chave AWS estática no `.env` da VM | IAM role da task ECS + SSM SecureString |
| CI/CD | Inexistente | CodePipeline V2 + CodeBuild |
| Monitoramento | Nenhum | CloudWatch Logs, alarmes, SNS, AWS Budget |
