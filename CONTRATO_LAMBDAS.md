# Contrato das lambdas de leitura e interpretação de ECG — v1.0

Fonte da verdade do contrato entre o backend Django e as duas lambdas:

| Lambda | Mock (este repo, `mock_lambdas/`) | Real (Chris, OpenCV 5) |
|---|---|---|
| **Leitura** (`operation: "read"`) | `ecg-reading-mock` | substitui o mock mantendo este contrato |
| **Interpretação** (`operation: "interpret"`) | `ecg-interpretation-mock` | substitui o mock mantendo este contrato |

O backend escolhe a função pelo nome (env `AWS_LAMBDA_ECG_READING` / `AWS_LAMBDA_ECG_INTERPRETATION`);
trocar o mock pela real é só trocar o nome. O catálogo de códigos está em código em
[`mock_lambdas/ecg_contract/__init__.py`](mock_lambdas/ecg_contract/__init__.py), e a lambda real pode
reaproveitar esse módulo. Um teste falha se algum código do catálogo faltar neste documento.

## 1. Regras gerais

- Invocação **síncrona** (`InvocationType=RequestResponse`), payload e resposta em JSON. O limite da AWS é 6 MB
  em cada sentido, por isso a imagem vai por referência ao S3 e o sinal vai inline (12 derivações a 500 Hz ≈ 150–450 KB).
- **Erros esperados nunca viram `FunctionError`.** A lambda sempre devolve o envelope abaixo. `FunctionError`
  (exceção não tratada, timeout, OOM) significa falha de infraestrutura e o backend trata como `INTERNAL_ERROR`.
- `request_id` é obrigatório e precisa ser um UUID: use o id da tentativa (`ExamAttempt.id`). Ele vai para os
  logs e para o prefixo dos artefatos no S3.
- `contract_version` é opcional e vale `"1.0"` por padrão. Outro valor devolve `UNSUPPORTED_CONTRACT_VERSION`.
- Campos desconhecidos são ignorados. Isso permite repassar a saída da leitura direto na interpretação.
- A validação de entrada roda **antes** de qualquer processamento. Uma entrada inválida devolve erro de
  validação mesmo quando um cenário mock foi pedido.
- Mensagens em pt-BR, prontas para mostrar ao usuário. Use `code` para decidir o fluxo e `message` para exibir.

### 1.1 Envelope de resposta (as duas lambdas)

```json
{
  "contract_version": "1.0",
  "operation": "read | interpret",
  "request_id": "uuid recebido (null se o próprio request_id for inválido)",
  "status": "OK | OK_WITH_WARNINGS | REFUSED | ERROR",
  "warnings": [{"code": "LEAD_PARTIAL_TRACE", "message": "…", "lead": "V2"}],
  "error": null,
  "result": { "…": "específico da operação; null quando status é REFUSED/ERROR" },
  "meta": {"engine": "ecg-reading-mock", "engine_version": "git sha", "duration_ms": 235,
           "mock_scenario": "success (só nos mocks)"}
}
```

| `status` | Significado | `result` | `error` | O que o app faz |
|---|---|---|---|---|
| `OK` | Sucesso sem ressalvas | preenchido | `null` | segue o fluxo |
| `OK_WITH_WARNINGS` | Sucesso com avisos (`warnings` não vazio) | preenchido | `null` | segue, exibindo os avisos |
| `REFUSED` | **Só leitura.** Empecilho visível na foto | `null` | preenchido (`category: "refused"`) | pede nova foto e mostra o motivo e a prévia |
| `ERROR` | Entrada inválida ou falha de processamento | `null` | preenchido | mostra a mensagem; `retryable` diz se vale tentar de novo |

Formato de `error`:

```json
{"code": "READ_GRID_NOT_FOUND", "category": "validation | refused | processing | internal",
 "retryable": false, "message": "Mensagem legível em pt-BR.", "details": {"…": "contexto opcional"}}
```

## 2. Lambda de leitura (`read`)

### 2.1 Request

```json
{
  "contract_version": "1.0",
  "request_id": "3f6c1f9e-8f0a-4c1e-9a52-0d2b7e0b6a11",
  "image": {"bucket": "ecg-lambdas-images-785236377332-sa-east-1", "key": "<user>/<ts>/<exam>/<attempt>/original.jpg"},
  "layout": "3x4+1",
  "calibration": {"speed_mm_s": 25, "gain_mm_mv": 10},
  "mock": {"scenario": "refused", "code": "READ_REFUSED_BLURRY"}
}
```

| Campo | Obrigatório | Regra | Erro |
|---|---|---|---|
| `image.bucket`, `image.key` | sim | strings não vazias | `IMAGE_MISSING` |
| objeto no S3 | sim | precisa existir | `IMAGE_NOT_FOUND` |
| tamanho | — | ≤ 15 MB | `IMAGE_TOO_LARGE` |
| formato (pelo conteúdo, não pela extensão) | — | JPEG, PNG ou WebP | `IMAGE_FORMAT_UNSUPPORTED` / `IMAGE_UNREADABLE` |
| resolução (após EXIF) | — | lado maior ≥ 800 px e lado menor ≥ 500 px | `IMAGE_TOO_SMALL` |
| `layout` | sim | um dos layouts da tabela 2.2 | `LAYOUT_MISSING` / `LAYOUT_UNSUPPORTED` |
| `calibration` | não (padrão 25 mm/s, 10 mm/mV) | `speed_mm_s` ∈ {12.5, 25, 50}; `gain_mm_mv` ∈ {5, 10, 20} | `CALIBRATION_INVALID` |
| `mock` | não | só nos mocks; a real ignora (ver seção 4) | `MOCK_SCENARIO_INVALID` |

### 2.2 Layouts suportados

Cada linha da grade ocupa 10 s de papel, dividida igualmente entre as colunas. As derivações de ritmo ocupam 10 s cada.

| `layout` | Grade (linhas) | Ritmo (10 s) |
|---|---|---|
| `3x4` | I aVR V1 V4 · II aVL V2 V5 · III aVF V3 V6 (2,5 s cada) | — |
| `3x4+1` | idem | II |
| `3x4+3` | idem | V1, II, V5 |
| `6x2` | I V1 · II V2 · III V3 · aVR V4 · aVL V5 · aVF V6 (5 s cada) | — |
| `6x2+1` | idem | II |
| `12x1` | uma derivação por linha (10 s cada) | — |

### 2.3 Response `result` (status `OK` / `OK_WITH_WARNINGS`)

```json
{
  "layout": "3x4+1",
  "calibration": {"speed_mm_s": 25.0, "gain_mm_mv": 10.0},
  "sampling_rate_hz": 500,
  "units": "mV",
  "grid": {"small_box_px": 5.888, "large_box_px": 29.44, "origin_px": [64.0, 60.0],
           "rotation_deg": 1.8, "paper_bbox_px": [64.0, 60.0, 1536.0, 960.0]},
  "leads": [
    {"name": "I", "kind": "panel", "row": 0, "col": 0, "start_s": 0.0, "duration_s": 2.5,
     "quality": "good", "coverage": 1.0, "bbox_px": [64.0, 60.0, 432.0, 285.0], "baseline_y_px": 172.5,
     "samples": [0.0, 0.0012, 0.0031, "… 1250 valores (mV) ou null"]},
    {"name": "II", "kind": "rhythm", "row": 3, "col": 0, "start_s": 0.0, "duration_s": 10.0, "…": "…"}
  ],
  "artifacts": {
    "overlay": {"bucket": "ecg-lambdas-results-…", "key": "reading/<request_id>/<run_id>/overlay.png", "content_type": "image/png"},
    "signal":  {"bucket": "ecg-lambdas-results-…", "key": "reading/<request_id>/<run_id>/signal.json", "content_type": "application/json"}
  }
}
```

- `leads[].samples`: valores em **mV** na taxa `sampling_rate_hz`, com tamanho `duration_s × sampling_rate_hz`.
  Use **`null`** nos trechos sem leitura (nunca 0 inventado). `start_s` é o instante no papel (0–10 s), e as
  derivações da mesma coluna são simultâneas.
- `kind`: `panel` (célula da grade) ou `rhythm` (tira longa). A mesma derivação pode aparecer nos dois.
- `quality`: `good` | `partial` | `saturated` | `low`. `coverage`: fração de amostras não nulas (0–1).
- `grid`, `bbox_px` e `baseline_y_px` são coordenadas em pixels **da imagem original após a rotação EXIF**,
  para o app desenhar ou destacar regiões, se quiser.
- `artifacts.overlay`: **PNG da imagem original com o gridline detectado e o traço extraído desenhados por cima.**
  É a imagem que o usuário vê para aceitar ou recusar a leitura. O backend gera a presigned URL; a lambda
  nunca devolve URL.
- `artifacts.signal`: o mesmo `result` (sem `artifacts`), persistido para auditoria e reprocessamento.
- Os artefatos ficam sempre no bucket de resultados (env `RESULTS_BUCKET` da lambda) e sob
  `reading/<request_id>/<run_id>/`. Cada execução gera um `run_id` novo, então nada é sobrescrito.

### 2.4 Recusa (`status: "REFUSED"`)

A leitura é recusada quando há **empecilho visível** que o usuário resolve tirando outra foto. O `error` traz o motivo e,
quando possível, uma prévia com a região do problema destacada:

```json
{"code": "READ_REFUSED_OCCLUDED", "category": "refused", "retryable": false,
 "message": "Há sobreposição sobre o traçado (dedo, reflexo, objeto ou anotação); remova e fotografe novamente.",
 "details": {"region_px": [560, 300, 960, 700],
             "preview": {"bucket": "…results…", "key": "reading/<request_id>/<run_id>/refusal_preview.png", "content_type": "image/png"}}}
```

Diferença entre `REFUSED` e `ERROR` de processamento: na recusa **a foto** tem problema e a solução é outra
foto. No `ERROR` com `category: "processing"` **o algoritmo** não conseguiu ler uma foto aparentemente boa.

## 3. Lambda de interpretação (`interpret`)

### 3.1 Request

```json
{
  "contract_version": "1.0",
  "request_id": "3f6c1f9e-8f0a-4c1e-9a52-0d2b7e0b6a11",
  "patient": {"sex": "F"},
  "signal": {"sampling_rate_hz": 500, "units": "mV",
             "leads": [{"name": "II", "samples": [0.0, 0.01, "…"]}, "…"]},
  "mock": {"scenario": "success_with_warnings"}
}
```

`signal` tem o mesmo formato de `result` da leitura: o backend pode repassar o `result` da leitura inteiro, porque campos extras são ignorados.

| Campo | Regra | Erro |
|---|---|---|
| `patient.sex` | obrigatório, `"F"` ou `"M"` (o backend mapeia `Patient.gender`) | `SEX_MISSING` / `SEX_INVALID` |
| `signal` | objeto obrigatório | `SIGNAL_MISSING` / `SIGNAL_INVALID` |
| `signal.sampling_rate_hz` | número entre 100 e 2000 | `SIGNAL_SAMPLING_RATE_INVALID` |
| `signal.units` | opcional; se vier, `"mV"` | `SIGNAL_INVALID` |
| `signal.leads` | lista não vazia | `SIGNAL_MISSING` |
| `leads[].name` | uma das 12 derivações padrão (I, II, III, aVR, aVL, aVF, V1–V6) | `SIGNAL_LEAD_UNKNOWN` |
| `leads[].samples` | lista de números ou `null`, no máximo 60 s | `SIGNAL_INVALID` |
| amostras válidas | ≥ 1 s (`sampling_rate_hz` amostras não nulas) por derivação | `SIGNAL_TOO_SHORT` |

Derivações ausentes **não** são erro: geram o aviso `LEAD_MISSING`, e os critérios que dependem delas saem como `not_evaluated`.

### 3.2 Response `result`

```json
{
  "patient": {"sex": "F"},
  "summary": {
    "classification": "normal | borderline | abnormal",
    "headline": "ECG dentro dos limites da normalidade",
    "rhythm": "Ritmo sinusal",
    "heart_rate_bpm": 72,
    "key_findings": ["Ritmo sinusal", "Eixo elétrico normal"],
    "text": "Ritmo sinusal, FC 72 bpm, QTc 416 ms. ECG dentro dos limites da normalidade."
  },
  "details": {
    "measurements": {
      "heart_rate_bpm": {"value": 72, "unit": "bpm", "confidence": "high", "lead": "II"},
      "rr_ms": {"value": 833, "unit": "ms", "confidence": "high"},
      "pr_ms": {"value": 160, "unit": "ms", "confidence": "medium"},
      "qrs_ms": {"value": 92, "unit": "ms", "confidence": "medium"},
      "qt_ms": {"value": 380, "unit": "ms", "confidence": "medium"},
      "qtc_ms": {"value": 416, "unit": "ms", "confidence": "medium", "formula": "Bazett"},
      "qrs_axis_deg": {"value": 45, "unit": "deg", "confidence": "medium"}
    },
    "findings": [{"code": "SINUS_RHYTHM", "label": "Ritmo sinusal", "severity": "info | warning | critical",
                  "evidence": "FC 72 bpm, RR regular"}],
    "criteria": [{"name": "QTc (Bazett)", "value": 416, "unit": "ms", "reference": "≤ 460 ms (feminino)",
                  "sex_specific": true, "status": "normal | abnormal | not_evaluated"}],
    "per_lead": [{"name": "I", "r_amplitude_mv": 0.711, "s_amplitude_mv": 0.321, "quality": "good"}]
  },
  "disclaimer": "Resultado gerado automaticamente a partir do sinal extraído. Não substitui a avaliação e o laudo de um médico."
}
```

- `summary` é o **resumo geral** (tela principal). `details` é o **resultado detalhado**.
- `confidence`: `high` | `medium` | `low`. `measurements.*.value` pode ser `null` quando não for medível.
- O sexo entra nos critérios com limiar dependente de sexo (`sex_specific: true`). No mock: QTc ≤ 460 ms (F) ou
  ≤ 450 ms (M), e Cornell ≤ 2,0 mV (F) ou ≤ 2,8 mV (M). Sokolow-Lyon (≤ 3,5 mV) não depende de sexo.
- `findings[].code` é um identificador estável em `UPPER_SNAKE_CASE`. O mock emite `SINUS_RHYTHM`,
  `SINUS_BRADYCARDIA`, `SINUS_TACHYCARDIA`, `NORMAL_AXIS`, `AXIS_DEVIATION`, `QTC_PROLONGED` e `LVH_VOLTAGE`.
  A lambda real pode acrescentar novos códigos, desde que documente aqui.
- O mock **mede** a FC e as amplitudes a partir do sinal recebido (picos R). PR, QRS, QT e o eixo são fixos.

## 4. Cenários de teste (só nas lambdas mock)

O cenário é escolhido por `mock.scenario` no payload. Sem esse campo, vale a env `MOCK_SCENARIO` da função e, na falta dela, `success`.
`mock.code` escolhe o código específico; sem ele, vale o padrão da tabela.

| `mock.scenario` | Leitura | Interpretação | `mock.code` aceito (padrão em **negrito**) |
|---|---|---|---|
| `success` | `OK`, sinal sintético 72 bpm e overlay | `OK`, ECG normal | — |
| `success_with_warnings` | `OK_WITH_WARNINGS`: V2 parcial (`null`), aVL saturada e rotação corrigida | `OK_WITH_WARNINGS`: V2 de baixa qualidade e QT pouco confiável | — |
| `failure` | `ERROR` | `ERROR` | leitura: **`READ_TRACE_EXTRACTION_FAILED`**, `READ_GRID_NOT_FOUND`, `STORAGE_ERROR`, `INTERNAL_ERROR` · interpretação: **`INTERP_NO_QRS_DETECTED`**, `INTERP_SIGNAL_TOO_NOISY`, `INTERNAL_ERROR` |
| `refused` | `REFUSED`, com prévia | — (inválido: `MOCK_SCENARIO_INVALID`) | **`READ_REFUSED_BLURRY`**, `READ_REFUSED_CROPPED`, `READ_REFUSED_OCCLUDED`, `READ_REFUSED_LOW_CONTRAST`, `READ_REFUSED_NOT_ECG`, `READ_REFUSED_LAYOUT_MISMATCH` |

Os erros de validação (seções 2.1 e 3.1) são reproduzidos com payloads inválidos de verdade, sem precisar de cenário.
O overlay do mock leva a marca "MOCK — sinal sintético", para não ser confundido com uma leitura real.

## 5. Catálogo de erros

`retryable: true` só para `STORAGE_ERROR` e `INTERNAL_ERROR` (falhas transitórias). Todos os outros têm `retryable: false`.

### Comuns
| Código | Categoria | Mensagem |
|---|---|---|
| `INVALID_PAYLOAD` | validation | O payload precisa ser um objeto JSON. |
| `UNSUPPORTED_CONTRACT_VERSION` | validation | Versão de contrato não suportada; use 1.0. |
| `REQUEST_ID_INVALID` | validation | request_id ausente ou inválido (use o UUID da tentativa). |
| `MOCK_SCENARIO_INVALID` | validation | Cenário de teste (mock) inválido. *(só nos mocks)* |
| `STORAGE_ERROR` | internal | Falha ao ler ou gravar artefatos no S3. |
| `INTERNAL_ERROR` | internal | Erro interno inesperado na lambda. |

### Leitura
| Código | Categoria | Mensagem |
|---|---|---|
| `IMAGE_MISSING` | validation | Informe a imagem do ECG (image.bucket e image.key). |
| `IMAGE_NOT_FOUND` | validation | A imagem informada não existe no S3. |
| `IMAGE_FORMAT_UNSUPPORTED` | validation | Formato de imagem não suportado; envie JPG, PNG ou WebP. |
| `IMAGE_TOO_LARGE` | validation | A imagem excede o tamanho máximo de 15 MB. |
| `IMAGE_TOO_SMALL` | validation | Resolução insuficiente; a imagem deve ter ao menos 800x500 px. |
| `IMAGE_UNREADABLE` | validation | Não foi possível decodificar a imagem enviada. |
| `LAYOUT_MISSING` | validation | Informe o layout de derivações (ex.: 3x4+1). |
| `LAYOUT_UNSUPPORTED` | validation | Layout de derivações não suportado. |
| `CALIBRATION_INVALID` | validation | Calibração inválida; velocidade 12.5/25/50 mm/s e ganho 5/10/20 mm/mV. |
| `READ_REFUSED_BLURRY` | refused | A imagem está borrada ou fora de foco; fotografe novamente com o papel nítido. |
| `READ_REFUSED_CROPPED` | refused | O traçado está cortado nas bordas; enquadre o papel inteiro na foto. |
| `READ_REFUSED_OCCLUDED` | refused | Há sobreposição sobre o traçado (dedo, reflexo, objeto ou anotação); remova e fotografe novamente. |
| `READ_REFUSED_LOW_CONTRAST` | refused | Contraste insuficiente entre o traçado e a grade; melhore a iluminação. |
| `READ_REFUSED_NOT_ECG` | refused | A imagem não parece ser um ECG em papel. |
| `READ_REFUSED_LAYOUT_MISMATCH` | refused | O layout detectado não corresponde ao layout informado. |
| `READ_GRID_NOT_FOUND` | processing | Não foi possível detectar a grade milimetrada do papel. |
| `READ_TRACE_EXTRACTION_FAILED` | processing | Não foi possível extrair o traçado das derivações. |

### Interpretação
| Código | Categoria | Mensagem |
|---|---|---|
| `SEX_MISSING` | validation | Informe o sexo do paciente (patient.sex). |
| `SEX_INVALID` | validation | Sexo do paciente inválido; use "F" ou "M". |
| `SIGNAL_MISSING` | validation | Informe o sinal do ECG (signal). |
| `SIGNAL_INVALID` | validation | Sinal do ECG em formato inválido. |
| `SIGNAL_SAMPLING_RATE_INVALID` | validation | Taxa de amostragem inválida; use entre 100 e 2000 Hz. |
| `SIGNAL_LEAD_UNKNOWN` | validation | Derivação desconhecida no sinal. |
| `SIGNAL_TOO_SHORT` | validation | Sinal curto demais; cada derivação precisa de ao menos 1 s. |
| `INTERP_NO_QRS_DETECTED` | processing | Nenhum complexo QRS detectado no sinal. |
| `INTERP_SIGNAL_TOO_NOISY` | processing | Sinal ruidoso demais para interpretação confiável. |

### Avisos (`warnings[].code`)
| Código | Lambda | Mensagem |
|---|---|---|
| `LEAD_PARTIAL_TRACE` | leitura | Traçado extraído parcialmente; trechos sem leitura vêm como null. |
| `LEAD_SATURATED` | leitura | Traçado encosta na derivação vizinha; amplitudes podem estar cortadas. |
| `IMAGE_ROTATION_CORRECTED` | leitura | A imagem estava inclinada e foi alinhada automaticamente. |
| `CALIBRATION_PULSE_NOT_FOUND` | leitura | Pulso de calibração não encontrado; usada a calibração informada. |
| `LEAD_MISSING` | interpretação | Derivação ausente no sinal; critérios que dependem dela foram omitidos. |
| `LEAD_LOW_QUALITY` | interpretação | Derivação com qualidade baixa; medidas nela são menos confiáveis. |
| `MEASUREMENT_UNRELIABLE` | interpretação | Medida calculada com baixa confiança. |

## 6. Requisitos para as lambdas reais

1. Mesmo envelope, mesmos nomes de campo, mesmos códigos. Um código novo entra primeiro neste documento e no
   catálogo (`ecg_contract`), com versão **minor** (1.1) se for compatível e **major** (2.0) se quebrar algo.
2. Ignorar o campo `mock`. `meta.engine` passa a identificar o algoritmo real (ex.: `ecg-reading-opencv5`).
3. Leitura: gravar `overlay.png` (imagem original + gridline + traço) e `signal.json` sob
   `reading/<request_id>/<run_id>/` no bucket de resultados. Na recusa, `refusal_preview.png` é recomendado, não obrigatório.
4. Não lançar exceção para erro esperado. Envolver o handler num `try/except` que devolve `INTERNAL_ERROR`.
5. Rodar a suíte `mock_lambdas/tests` trocando o handler pelo real: os testes de validação e de catálogo valem
   para as duas implementações. Os testes de cenário são exclusivos do mock.
6. Timeout: o backend espera até `AWS_LAMBDA_READ_TIMEOUT` (270 s), e a lambda deve terminar em ≤ 240 s.
