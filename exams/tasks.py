"""Asynchronous phase execution (docs/user-flow-v1.md D6).

`ECG_TASK_MODE=inline` (dev/tests) runs the phase right away, inside the request. `sqs` publishes
`{"ecg_task": "read|interpret", "attempt_id": "<uuid>"}` to the `ECG_TASK_QUEUE_URL` queue; the worker
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
    """Runs phase `task` of the attempt. Idempotent and never raises.

    Runs only if the attempt is in the phase's processing state: a duplicate or late event
    (attempt already completed, refused, in error) is ignored. Returns True if the phase ran.
    """
    if task not in TASKS:
        logger.error("Tarefa desconhecida %r (attempt_id=%s)", task, attempt_id)
        return False
    try:
        attempt = ExamAttempt.objects.select_related("exam__patient").get(pk=attempt_id)
    except (ExamAttempt.DoesNotExist, ValidationError, ValueError, TypeError):
        logger.warning("Task %s: attempt %s does not exist", task, attempt_id)
        return False

    processing = services.PHASES[task][0]
    if attempt.status != processing:
        logger.info("Task %s skipped: attempt %s is in %s", task, attempt_id, attempt.status)
        return False

    try:
        TASKS[task](attempt)
    except Exception:
        logger.exception("Task %s failed on attempt %s", task, attempt_id)
        try:
            services.fail_phase(attempt, task, "INTERNAL_ERROR")
        except Exception:
            logger.exception("Could not mark an error on attempt %s", attempt_id)
    return True


def dispatch(task, attempt):
    """Starts the phase of an attempt that just entered its processing state.

    If publishing to the queue fails, the attempt goes to an error state with a retryable `INTERNAL_ERROR`.
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
        logger.exception("Failed to publish task %s of attempt %s", task, attempt_id)
        services.fail_phase(attempt, task, "INTERNAL_ERROR")
