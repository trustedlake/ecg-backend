"""Phase queue worker (plan B4): `python manage.py ecg_worker`.

Long polling on `ECG_TASK_QUEUE_URL`. Each `{"ecg_task", "attempt_id"}` message runs through
`tasks.run_task` (which never raises) and is deleted only afterwards: if the container dies midway,
the message returns to the queue after the visibility timeout. An invalid body is logged and deleted.
SIGTERM/SIGINT finish the message in progress and exit with code 0 (ECS stopTimeout: 120 s).
`ECG_WORKER_CONCURRENCY` threads long-poll in parallel: the real reading waits for the lambda
for 50-190 s and must not hold up the whole queue. A reading cut off by stopTimeout returns to the
queue after the visibility timeout and is redone.
"""

import json
import logging
import signal
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from exams import tasks

logger = logging.getLogger("exams.worker")

WAIT_TIME_SECONDS = 20
ERROR_BACKOFF_SECONDS = 5


def parse_body(body):
    """Returns (task, attempt_id) or None if the body is not a valid task."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    task, attempt_id = data.get("ecg_task"), data.get("attempt_id")
    if task not in tasks.TASKS or not isinstance(attempt_id, str) or not attempt_id:
        return None
    return task, attempt_id


class Worker:
    def __init__(self, queue_url, client=None):
        self.queue_url = queue_url
        self.client = client or tasks._sqs_client()
        self.stopping = False

    def stop(self, signum=None, frame=None):
        logger.info("Signal %s received; stopping after the current message", signum)
        self.stopping = True

    def handle_message(self, message):
        parsed = parse_body(message.get("Body"))
        if parsed is None:
            logger.error("Message %s has an invalid body; deleting: %.200r",
                         message.get("MessageId"), message.get("Body"))
        else:
            task, attempt_id = parsed
            started = time.monotonic()
            close_old_connections()
            try:
                ran = tasks.run_task(task, attempt_id)
            finally:
                close_old_connections()
            logger.info("task=%s attempt_id=%s ran=%s duration=%.1fs",
                        task, attempt_id, ran, time.monotonic() - started)
        # Delete only after processing (or discarding an invalid body).
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])

    def poll_once(self):
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=WAIT_TIME_SECONDS,
        )
        for message in response.get("Messages", []):
            if self.stopping:
                # A signal arrived during long polling: hand the message back to another worker
                # instead of starting a phase that may run past stopTimeout.
                self.client.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=0,
                )
                continue
            self.handle_message(message)

    def run(self):
        logger.info("Worker escutando %s", self.queue_url)
        while not self.stopping:
            try:
                self.poll_once()
            except Exception:
                logger.exception("Worker loop error; retrying in %ss", ERROR_BACKOFF_SECONDS)
                time.sleep(ERROR_BACKOFF_SECONDS)
        logger.info("Worker encerrado")


class Command(BaseCommand):
    help = "Consumes the SQS phase queue (reading/interpretation) and runs each task."

    def handle(self, *args, **options):
        if not settings.ECG_TASK_QUEUE_URL:
            raise CommandError("ECG_TASK_QUEUE_URL is not set.")
        worker = Worker(settings.ECG_TASK_QUEUE_URL)
        signal.signal(signal.SIGTERM, worker.stop)
        signal.signal(signal.SIGINT, worker.stop)
        concurrency = max(1, settings.ECG_WORKER_CONCURRENCY)
        if concurrency == 1:
            worker.run()
            return
        threads = [threading.Thread(target=worker.run, name=f"ecg-worker-{i}") for i in range(concurrency)]
        for thread in threads:
            thread.start()
        # join with a timeout: the signal is only handled on the main thread between waits.
        for thread in threads:
            while thread.is_alive():
                thread.join(1)
