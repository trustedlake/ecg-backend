# Integração ECG — contrato de uma Lambda

Aplicação: Expo Go / Expo Web → Django (Oracle VM) → S3 e AWS Lambda `ecg-analyser`.

| Operação | Entrada | Status esperado | Chaves de artefato |
|---|---|---|---|
| analyze | `operation`, `attempt_id`, `images_bucket`, `imagem_s3_key`, `results_bucket`, `calibration:{speed_mm_s,gain_mm_mv}` | `CONSISTENCY_PASS`, `REVIEW_REQUIRED`, `REJECTED_LOW_TRACE_GRID_CONTRAST` | `ecg-analyser/{attempt_id}/initial/{result.json,signals.npz,trace_masks.npz,working_image.png,visao_geral.png}`; rejeição só `result.json` e `rejection_preview.png` |
| refine | `operation`, `attempt_id`, `results_bucket`, `lead`, `strokes`, `parent_artifacts` | `MANUAL_REVIEW_REQUIRED` | `ecg-analyser/{attempt_id}/{uuid}/{result.json,signals.npz,trace_masks.npz,working_image.png,correction_preview.png}` |

`POST /api/exams/{exam_id}/attempts/{attempt_id}/process/` invoca **sincronamente** via boto3 `RequestResponse`. `GET` consulta estado persistido, não faz polling do runtime da Lambda. Não há fila assíncrona hoje. Tempo de execução por foto **não medido em AWS**. Lambda até 240s; cliente boto3 read_timeout 270s (`AWS_LAMBDA_READ_TIMEOUT`); Gunicorn timeout 360s (Dockerfile) e proxy read_timeout ≥ 400s, CDN/LB e cliente com janela compatível. Alguns proxies/CDNs têm limite máximo inferior: nesses ambientes, migrar para job assíncrono (SQS/Lambda + status em DB). Cold start consome essa janela.

Aprovação confirma **extração**, sem laudo clínico. Rejeição por contraste proíbe refine/aprovação. Mudanças manuais mantêm revisão necessária. Refine usa transição de estado para bloquear chamadas concorrentes; se o processo Django cair durante a Lambda, a tentativa fica em `fase_a_processando`; após `STALE_PROCESSING_SECONDS` (padrão 600s, maior que o timeout do Gunicorn) o próximo acesso a ela a recupera: volta para `aguardando_aprovacao` se já havia uma análise salva (refine interrompido) ou vai para `erro` (análise interrompida; capturar nova foto).

`infra-oracle/` é a implantação-alvo do Django (Oracle VM). A infraestrutura `infra/` EC2/Terraform é legada: não reaplicar. Arquivos de infraestrutura não são incluídos em patch do backend.

## Contrato v1 de duas lambdas (leitura + interpretação)

O fluxo novo separa **leitura** (imagem + layout → sinal + overlay com gridline, podendo recusar a foto) e
**interpretação** (sinal + sexo → resumo + detalhado). O contrato está em [`CONTRATO_LAMBDAS.md`](CONTRATO_LAMBDAS.md).
As lambdas mock `ecg-reading-mock` e `ecg-interpretation-mock` (`mock_lambdas/`) o implementam com resultados
sintéticos e cenários selecionáveis, para validar o fluxo antes dos scripts reais. O backend ainda chama
`ecg-analyser`; a troca é feita no card de migração.
