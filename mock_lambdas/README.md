# Lambdas mock de leitura e interpretação de ECG

Implementam o **contrato v1** ([`../CONTRATO_LAMBDAS.md`](../CONTRATO_LAMBDAS.md)) com resultados sintéticos,
para validar o fluxo app → backend → lambdas antes dos algoritmos reais (OpenCV 5).

| Função AWS | Código | Faz de verdade | É sintético |
|---|---|---|---|
| `ecg-reading-mock` | `reading/handler.py` | valida payload, lê e decodifica a foto no S3, desenha grade e traço sobre ela, grava `overlay.png` e `signal.json` | o sinal (72 bpm) e a posição da grade |
| `ecg-interpretation-mock` | `interpretation/handler.py` | valida sexo e sinal, mede FC e amplitudes no sinal recebido, aplica limiares por sexo | PR, QRS, QT e eixo |

`ecg_contract/` (envelope, catálogo de erros/avisos, layouts) é copiado para os dois pacotes e pode ser reaproveitado pelas lambdas reais.

## Cenários

`"mock": {"scenario": "success | success_with_warnings | failure | refused", "code": "<opcional>"}` no payload.
Sem esse campo, vale a env `MOCK_SCENARIO` da função (variável Terraform `mock_scenario`) e, na falta dela, `success`.
A tabela completa está na seção 4 do contrato.

## Testes

```bash
pip install pillow boto3
python -m unittest discover -s mock_lambdas/tests -v   # offline, S3 falso
python mock_lambdas/smoke_aws.py                       # contra as funções deployadas (profile trustedlake)
```

## Deploy (sem Docker)

```bash
mock_lambdas/build.sh                                   # gera build/{reading,interpretation}/ (Pillow arm64)
cd mock_lambdas/infra
terraform init
terraform plan  -var="code_version=$(git rev-parse --short HEAD)" -out=tfplan
terraform apply tfplan
```

Stack independente (state `s3://ecg-lambdas-terraform-state/ecg-mock-lambdas/terraform.tfstate`). Ela só
**lê** os buckets do `ecg-lambdas` e não altera nada lá. Runtime python3.12 arm64, logs no CloudWatch com
retenção de 30 dias. O principal IAM do backend precisa de `lambda:InvokeFunction` nos ARNs em `terraform output function_arns`.

Para trocar pelas lambdas reais, aponte `AWS_LAMBDA_ECG_READING` / `AWS_LAMBDA_ECG_INTERPRETATION` no backend para as novas funções.
