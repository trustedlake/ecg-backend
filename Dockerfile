# Imagem única do backend ECG (plano B4): container `web` (CMD padrão, gunicorn) e
# container `worker` (task definition sobrescreve o command: python manage.py ecg_worker).
# Build: docker build --platform linux/arm64 -t ecg-backend:local .
# O export web do Expo, se existir em frontend_dist/, entra na imagem e é servido pelo Django.

FROM public.ecr.aws/docker/library/python:3.12-slim AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

FROM public.ecr.aws/docker/library/python:3.12-slim
ARG APP_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/venv/bin:$PATH \
    APP_VERSION=$APP_VERSION

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=deps /venv /venv
COPY --chown=app:app . /app

# collectstatic no build (admin/DRF + manifest do WhiteNoise); a chave é fictícia e só existe aqui.
RUN DJANGO_SECRET_KEY=build-only-not-secret DEBUG=False \
    python manage.py collectstatic --noinput \
    && chown -R app:app /app/staticfiles

USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-"]
