"""Worker da fila de fases (plano B4): `python manage.py ecg_worker`.

Long polling em `ECG_TASK_QUEUE_URL`. Cada mensagem `{"ecg_task", "attempt_id"}` roda via
`tasks.run_task` (que nunca levanta exceção) e só depois é apagada: se o container morrer no meio,
a mensagem volta para a fila após o visibility timeout. Corpo inválido é logado e apagado.
SIGTERM/SIGINT terminam a mensagem em andamento e saem com código 0 (stopTimeout do ECS: 120 s).
`ECG_WORKER_CONCURRENCY` threads fazem o long polling em paralelo: a leitura real espera a lambda
por 50–190 s e não pode segurar a fila inteira. Uma leitura cortada pelo stopTimeout volta para a
fila depois do visibility timeout e é refeita.
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
    """Devolve (task, attempt_id) ou None se o corpo não for uma tarefa válida."""
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
        logger.info("Sinal %s recebido; encerrando após a mensagem atual", signum)
        self.stopping = True

    def handle_message(self, message):
        parsed = parse_body(message.get("Body"))
        if parsed is None:
            logger.error("Mensagem %s com corpo inválido; apagando: %.200r",
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
        # Só apaga depois de processar (ou descartar um corpo inválido).
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])

    def poll_once(self):
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=WAIT_TIME_SECONDS,
        )
        for message in response.get("Messages", []):
            if self.stopping:
                # Sinal chegou durante o long polling: devolve a mensagem para outro worker
                # em vez de começar uma fase que pode passar do stopTimeout.
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
                logger.exception("Erro no loop do worker; nova tentativa em %ss", ERROR_BACKOFF_SECONDS)
                time.sleep(ERROR_BACKOFF_SECONDS)
        logger.info("Worker encerrado")


class Command(BaseCommand):
    help = "Consome a fila SQS de fases (leitura/interpretação) e executa cada tarefa."

    def handle(self, *args, **options):
        if not settings.ECG_TASK_QUEUE_URL:
            raise CommandError("ECG_TASK_QUEUE_URL não definida.")
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
        # join com timeout: o sinal só é tratado na thread principal entre as esperas.
        for thread in threads:
            while thread.is_alive():
                thread.join(1)
