"""Execução assíncrona das fases (docs/fluxo-usuario-v1.md D6).

`ECG_TASK_MODE=inline` (dev/testes) roda a fase na hora, dentro do request. `sqs` publica
`{"ecg_task": "read|interpret", "attempt_id": "<uuid>"}` na fila `ECG_TASK_QUEUE_URL`; o worker
(`manage.py ecg_worker`, etapa B4) consome e chama `run_task`.
"""

import json
import logging

from django.conf import settings
from django.core.exceptions import ValidationError

from . import services
from .models import ExamAttempt

logger = logging.getLogger(__name__)

TASKS = {
    "read": services.run_reading,
    "interpret": services.run_interpretation,
}


def _sqs_client():
    return services._session().client("sqs")


def run_task(task, attempt_id):
    """Executa a fase `task` da tentativa. Idempotente e nunca levanta exceção.

    Só roda se a tentativa estiver no `*_processando` da fase: um evento duplicado ou atrasado
    (tentativa já concluída, recusada, em erro) é ignorado. Devolve True se a fase rodou.
    """
    if task not in TASKS:
        logger.error("Tarefa desconhecida %r (attempt_id=%s)", task, attempt_id)
        return False
    try:
        attempt = ExamAttempt.objects.select_related("exam__patient").get(pk=attempt_id)
    except (ExamAttempt.DoesNotExist, ValidationError, ValueError, TypeError):
        logger.warning("Tarefa %s: tentativa %s não existe", task, attempt_id)
        return False

    processing = services.PHASES[task][0]
    if attempt.status != processing:
        logger.info("Tarefa %s ignorada: tentativa %s está em %s", task, attempt_id, attempt.status)
        return False

    try:
        TASKS[task](attempt)
    except Exception:
        logger.exception("Tarefa %s falhou na tentativa %s", task, attempt_id)
        try:
            services.fail_phase(attempt, task, "INTERNAL_ERROR")
        except Exception:
            logger.exception("Não foi possível marcar erro na tentativa %s", attempt_id)
    return True


def dispatch(task, attempt):
    """Dispara a fase de uma tentativa que acabou de entrar no `*_processando` dela.

    Se a publicação na fila falhar, a tentativa vai para `erro_*` com `INTERNAL_ERROR` retryable.
    """
    attempt_id = str(attempt.pk)
    if settings.ECG_TASK_MODE != "sqs":
        run_task(task, attempt_id)
        return
    try:
        _sqs_client().send_message(
            QueueUrl=settings.ECG_TASK_QUEUE_URL,
            MessageBody=json.dumps({"ecg_task": task, "attempt_id": attempt_id}),
        )
    except Exception:
        logger.exception("Falha ao publicar a tarefa %s da tentativa %s", task, attempt_id)
        services.fail_phase(attempt, task, "INTERNAL_ERROR")
