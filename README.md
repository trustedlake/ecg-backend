# ECG Backend — Django + Lambda única

Este servidor atende ao frontend **Expo Go / Expo Web**. Utiliza o ECG Analyser como uma única Lambda para `analyze` e `refine`. Não retorna diagnóstico clínico automático.

## Fluxo

1. `POST /api/auth/login/` recebe `username` e `password` e retorna `token` (DRF).
2. `POST /api/exams/` cria o exame e os dados do paciente.
3. `POST /api/exams/<exam_id>/attempts/` cria tentativa com `filename`, `content_type`, `speed_mm_s` e `gain_mm_mv`; devolve presigned PUT. Formatos: JPG, PNG, WebP.
4. Expo envia **uma foto** ao S3 usando `PUT` com `Content-Type` idêntico ao assinado.
5. `POST /api/exams/<exam_id>/attempts/<attempt_id>/process/` chama uma Lambda `analyze` e persiste artefatos; o `GET` na mesma rota recupera o estado sem reexecutar.
6. `POST .../refine/` envia uma única derivação e pinceladas de inclusão/exclusão; a Lambda produz uma nova versão sem apagar arquivos anteriores. A imagem usada para pintura é a imagem de trabalho.
7. `POST .../rejeitar/` rejeita um resultado que aguarda revisão; `POST .../aprovar/` confirma **a extração dos traçados**, não um laudo médico.
8. `GET /api/exams/<exam_id>/` lista histórico e resultado; `POST /api/attempts/<attempt_id>/feedback/` registra feedback.

Se a Lambda rejeitar uma imagem por contraste insuficiente, a mesma tentativa não pode ser refinada/aprovada: capture uma nova imagem.

## Configuração

Requisitos: Python 3.12+, Django 5.2, REST Framework, AWS Boto3, PostgreSQL ou SQLite de desenvolvimento.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Defina `DJANGO_SECRET_KEY` fora do ambiente de desenvolvimento, `DATABASE_URL`, `AWS_REGION`, `AWS_IMAGES_BUCKET`, `AWS_RESULTS_BUCKET`, `AWS_LAMBDA_ECG_ANALYSER`, origens de `CORS_ALLOWED_ORIGINS`, `ALLOWED_HOSTS` e credenciais/IAM AWS. **Não use `DEBUG=True` em produção.** Uma configuração sem secret real é rejeitada com `DEBUG=False`.

Para Expo Web, autorize no Django e no CORS do S3 a origem exata do navegador. Para Expo Go no celular, use IP da rede local ou domínio HTTPS acessível ao telefone. `localhost` se refere ao próprio celular.

A Lambda precisa de `s3:GetObject` nos dois buckets e `s3:PutObject` no bucket de resultados; o Django precisa das permissões de invocação e geração de URLs pré-assinadas. A imagem de deploy é montada com o `Dockerfile.lambda` do ECG Analyser. **Nenhum deploy AWS é realizado pelo ZIP.**

## Testes

```bash
python -m unittest discover -s tests -v  # 5 testes de serviço isolados, sem Django
python manage.py test                     # suíte de views/modelos, requer dependências
python manage.py check --deploy --fail-level WARNING  # após configurar produção
```

A suíte `manage.py test` e a execução real de AWS exigem instalações e serviços não disponíveis no ambiente da auditoria. Para uso clínico, ainda são necessárias validação médica, tratamento de dados pessoais e processos regulatórios apropriados.

Veja `INTEGRACAO_ECG.md` para contratos e limites adicionais. O contrato v1 das lambdas de leitura e interpretação (e as lambdas mock que o implementam) ficam no repo [tl-gmiler/ecg-lambdas-v1](https://github.com/tl-gmiler/ecg-lambdas-v1) ([`CONTRATO_LAMBDAS.md`](https://github.com/tl-gmiler/ecg-lambdas-v1/blob/main/CONTRATO_LAMBDAS.md)).


## Atualização para uma Lambda (não executada na AWS)

Veja `INTEGRACAO_ECG.md`. Django opera na Oracle VM; S3/Lambda permanecem na AWS. `smoke_e2e.py` executa somente sob autorização explícita, cria dados e requer uma fotografia-teste real. Configure Gunicorn/proxy para POST síncrono longo ou implemente fila assíncrona. Valide migrations e `manage.py check --deploy` no ambiente de produção.
