FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 1 worker: mesma config de producao hoje (systemd usa --workers 1) - timeout
# alto porque chamadas as lambdas da Fase B podem demorar.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--timeout", "960", "--workers", "1"]
