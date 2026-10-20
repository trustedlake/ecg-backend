"""Testes do worker SQS (`manage.py ecg_worker`, plano B4). SQS mockado; run_task mockado."""

import json
import signal
import threading
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from exams.management.commands import ecg_worker
from exams.management.commands.ecg_worker import Worker, parse_body

QUEUE = "https://sqs.sa-east-1.amazonaws.com/123/ecg-tasks"


def msg(body, handle="rh-1"):
    return {"MessageId": "m-1", "ReceiptHandle": handle, "Body": body}


def valid_body(task="read", attempt_id="3f1c"):
    return json.dumps({"ecg_task": task, "attempt_id": attempt_id})


class ParseBodyTests(SimpleTestCase):
    def test_valid(self):
        self.assertEqual(parse_body(valid_body("interpret", "abc")), ("interpret", "abc"))

    def test_invalid(self):
        for body in ["", "not json", "[]", "null", json.dumps({"ecg_task": "refine", "attempt_id": "x"}),
                     json.dumps({"ecg_task": "read"}), json.dumps({"ecg_task": "read", "attempt_id": 1})]:
            with self.subTest(body=body):
                self.assertIsNone(parse_body(body))


@mock.patch("exams.management.commands.ecg_worker.close_old_connections")
@mock.patch("exams.tasks.run_task", return_value=True)
class WorkerTests(SimpleTestCase):
    def make(self, *responses):
        client = mock.Mock()
        client.receive_message.side_effect = list(responses)
        return Worker(QUEUE, client=client), client

    def test_valid_message_runs_task_then_deletes(self, run_task, _close):
        order = []
        run_task.side_effect = lambda *a: order.append("run") or True
        worker, client = self.make({"Messages": [msg(valid_body("read", "a-1"))]})
        client.delete_message.side_effect = lambda **kw: order.append("delete")

        worker.poll_once()

        run_task.assert_called_once_with("read", "a-1")
        client.delete_message.assert_called_once_with(QueueUrl=QUEUE, ReceiptHandle="rh-1")
        self.assertEqual(order, ["run", "delete"])
        client.receive_message.assert_called_once_with(
            QueueUrl=QUEUE, MaxNumberOfMessages=1, WaitTimeSeconds=20)

    def test_invalid_body_is_deleted_without_running(self, run_task, _close):
        worker, client = self.make({"Messages": [msg("lixo")]})
        with self.assertLogs("exams.worker", "ERROR"):
            worker.poll_once()
        run_task.assert_not_called()
        client.delete_message.assert_called_once_with(QueueUrl=QUEUE, ReceiptHandle="rh-1")

    def test_message_not_deleted_if_processing_crashes(self, run_task, _close):
        # run_task never raises; if anything else fails, the message stays for redelivery.
        run_task.side_effect = RuntimeError("boom")
        worker, client = self.make({"Messages": [msg(valid_body())]})
        with self.assertRaises(RuntimeError):
            worker.poll_once()
        client.delete_message.assert_not_called()

    def test_empty_receive(self, run_task, _close):
        worker, client = self.make({})
        worker.poll_once()
        run_task.assert_not_called()
        client.delete_message.assert_not_called()

    def test_sigterm_finishes_current_message_then_stops(self, run_task, _close):
        worker, client = self.make({"Messages": [msg(valid_body())]}, {"Messages": [msg(valid_body(), "rh-2")]})

        def run_and_get_signal(*args):
            worker.stop(signal.SIGTERM)  # signal arrives mid-phase
            return True

        run_task.side_effect = run_and_get_signal
        worker.run()

        run_task.assert_called_once()
        client.delete_message.assert_called_once_with(QueueUrl=QUEUE, ReceiptHandle="rh-1")
        self.assertEqual(client.receive_message.call_count, 1)

    def test_signal_during_long_poll_returns_message_to_queue(self, run_task, _close):
        worker, client = self.make()

        def receive(**kwargs):
            worker.stop(signal.SIGTERM)
            return {"Messages": [msg(valid_body())]}

        client.receive_message.side_effect = receive
        worker.run()

        run_task.assert_not_called()
        client.delete_message.assert_not_called()
        client.change_message_visibility.assert_called_once_with(
            QueueUrl=QUEUE, ReceiptHandle="rh-1", VisibilityTimeout=0)

    @mock.patch("exams.management.commands.ecg_worker.time.sleep")
    def test_receive_error_backs_off_and_continues(self, sleep, run_task, _close):
        worker, client = self.make()
        calls = []

        def receive(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("rede")
            worker.stop(signal.SIGTERM)
            return {}

        client.receive_message.side_effect = receive
        with self.assertLogs("exams.worker", "ERROR"):
            worker.run()
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(ecg_worker.ERROR_BACKOFF_SECONDS)


class CommandTests(SimpleTestCase):
    @override_settings(ECG_TASK_QUEUE_URL="")
    def test_requires_queue_url(self):
        with self.assertRaises(CommandError):
            call_command("ecg_worker")

    @override_settings(ECG_TASK_QUEUE_URL=QUEUE, ECG_WORKER_CONCURRENCY=1)
    @mock.patch("exams.management.commands.ecg_worker.signal.signal")
    @mock.patch.object(Worker, "run")
    @mock.patch("exams.tasks._sqs_client")
    def test_installs_signal_handlers(self, _client, run, sig):
        call_command("ecg_worker")
        run.assert_called_once()
        self.assertEqual({c.args[0] for c in sig.call_args_list}, {signal.SIGTERM, signal.SIGINT})

    @override_settings(ECG_TASK_QUEUE_URL=QUEUE, ECG_WORKER_CONCURRENCY=3)
    @mock.patch("exams.management.commands.ecg_worker.signal.signal")
    @mock.patch("exams.tasks._sqs_client")
    def test_concurrency_runs_one_loop_per_thread(self, _client, _sig):
        threads = []

        def fake_run(worker):
            threads.append(threading.current_thread().name)

        with mock.patch.object(Worker, "run", fake_run):
            call_command("ecg_worker")
        self.assertEqual(sorted(threads), ["ecg-worker-0", "ecg-worker-1", "ecg-worker-2"])
