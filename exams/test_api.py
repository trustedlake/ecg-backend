"""Testes de API do fluxo V1 (plano §0.1, fluxo §3–4), com ECG_TASK_MODE=inline.

As lambdas são mockadas em `services.invoke_contract` (envelopes do contrato); o sinal no S3 em
`services._read_json_from_s3`; presigned URLs em `services.presign_get_url`/`presign_put_url`.
"""

import json
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from . import services, tasks
from .models import Exam, ExamAttempt, ExamStatus, Patient

User = get_user_model()
RESULTS = settings.AWS_RESULTS_BUCKET
# leads.json do método escolhido: sem sampling_rate_hz (vem do método) e com o resumo por derivação.
SIGNAL = {"leads": [{"name": "II", "kind": "rhythm", "quality": "good", "coverage": 1.0, "samples": [0.0, 0.1]}]}


def env(op, rid, status, result=None, error=None, warnings=None):
    body = {"contract_version": "2.0", "operation": op, "request_id": rid, "status": status,
            "error": error, "result": result, "meta": {"engine": f"ecg-{op}", "engine_version": "abc123"}}
    if warnings is not None:
        body["warnings"] = warnings
    return body


def read_ok(rid, status="OK", warnings=None):
    """Leitura 2.0 com um único método OK; os avisos vêm no método (contrato §3)."""
    prefix = f"reading/{rid}/run1/baseline_peaks"
    method = {
        "method": "baseline_peaks", "version": "v1", "status": "OK", "reason": None,
        "selector_score": 87.5, "sampling_rate_hz": 150.0, "warnings": warnings or [],
        "files": {"json": {"bucket": RESULTS, "key": f"{prefix}/result.json"},
                  "image": {"bucket": RESULTS, "key": f"{prefix}/preview.png"},
                  "leads": {"bucket": RESULTS, "key": f"{prefix}/leads.json"}},
    }
    result = {"layout": "3x4+1", "calibration": {"speed_mm_s": 25.0, "gain_mm_mv": 10.0},
              "units": "mV", "methods": [method]}
    return env("read", rid, status, result=result)


def interp_ok(rid, status="OK", warnings=None):
    result = {
        "summary": {"classification": "normal", "headline": "ECG normal", "rhythm": "Ritmo sinusal",
                    "heart_rate_bpm": 72, "key_findings": ["Ritmo sinusal"], "text": "Ritmo sinusal, FC 72 bpm."},
        "details": {"measurements": {}, "findings": [], "criteria": [], "per_lead": []},
        "disclaimer": "Não substitui o laudo de um médico.",
    }
    return env("interpret", rid, status, result=result, warnings=warnings)


def error(op, rid, code, retryable, category="processing", status="ERROR", details=None):
    err = {"code": code, "category": category, "retryable": retryable, "message": f"msg {code}"}
    if details:
        err["details"] = details
    return env(op, rid, status, error=err)


@override_settings(ECG_TASK_MODE="inline", ECG_TEST_SCENARIOS_ENABLED=False)
class FlowTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="medica", email="m@t.local", password="senha-123",
                                             name="Médica", crm="CRM-1")
        self.client.force_authenticate(self.user)
        self.patient = Patient.objects.create(doctor=self.user, name="Ana", age=30, sex="F")
        self.exam = Exam.objects.create(patient=self.patient, doctor=self.user)
        self.responses = {}  # operação -> envelope ou função(rid) -> envelope
        self.calls = []

        def fake_invoke(function_name, payload, operation=None):
            self.calls.append((operation, payload))
            reply = self.responses[operation]
            return reply(payload["request_id"]) if callable(reply) else reply

        for target, kwargs in [
            ("invoke_contract", {"side_effect": fake_invoke}),
            ("_read_json_from_s3", {"return_value": SIGNAL}),
            ("presign_get_url", {"side_effect": lambda b, k, e: f"https://signed/{k}"}),
            ("presign_put_url", {"return_value": "https://signed/PUT"}),
        ]:
            patcher = mock.patch.object(services, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def new_attempt(self, **extra):
        body = {"filename": "ecg.jpg", "content_type": "image/jpeg", "layout": "3x4+1", **extra}
        resp = self.client.post(reverse("attempt-create", args=[self.exam.id]), body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.data["attempt_id"]

    def url(self, name, attempt_id, exam_id=None):
        return reverse(name, args=[exam_id or self.exam.id, attempt_id])

    def post(self, name, attempt_id):
        return self.client.post(self.url(name, attempt_id))

    def get_attempt(self, attempt_id):
        resp = self.client.get(self.url("attempt-detail", attempt_id))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def exam_status(self):
        self.exam.refresh_from_db()
        return self.exam.status


class HappyPathTest(FlowTestCase):
    def test_full_flow_until_concluded(self):
        aid = self.new_attempt()
        self.responses = {"read": read_ok, "interpret": interp_ok}

        resp = self.post("attempt-read", aid)
        self.assertEqual(resp.status_code, 202)
        data = resp.data
        self.assertEqual(data["status"], ExamStatus.AWAITING_VALIDATION)
        self.assertEqual(set(data), {
            "id", "exam_id", "status", "layout", "calibration", "original_image_url", "reading",
            "interpretation", "can_retry", "needs_new_photo", "created_at", "updated_at"})
        self.assertEqual(data["calibration"], {"speed_mm_s": 25, "gain_mm_mv": 10})
        self.assertEqual(data["reading"]["overlay_url"],
                         f"https://signed/reading/{aid}/run1/baseline_peaks/preview.png")
        self.assertEqual(data["reading"]["leads"], [{"name": "II", "kind": "rhythm", "quality": "good", "coverage": 1.0}])
        self.assertTrue(data["original_image_url"].startswith("https://signed/"))
        self.assertIsNone(data["interpretation"])
        self.assertNotIn("samples", json.dumps(data))
        self.assertEqual(self.exam_status(), ExamStatus.AWAITING_VALIDATION)

        resp = self.post("attempt-accept", aid)
        self.assertEqual(resp.status_code, 202)
        data = self.get_attempt(aid)
        self.assertEqual(data["status"], ExamStatus.COMPLETED)
        self.assertEqual(data["interpretation"]["summary"]["heart_rate_bpm"], 72)
        self.assertEqual(data["interpretation"]["disclaimer"], "Não substitui o laudo de um médico.")
        self.assertEqual((data["can_retry"], data["needs_new_photo"]), (False, False))
        self.assertEqual(self.exam_status(), ExamStatus.COMPLETED)
        self.assertEqual(self.calls[1][1]["patient"], {"sex": "F"})
        # O leads.json vai como signal, com a taxa do método escolhido (contrato 2.0 §4).
        self.assertEqual(self.calls[1][1]["signal"], {**SIGNAL, "sampling_rate_hz": 150.0, "units": "mV"})

        detail = self.client.get(reverse("exam-detail", args=[self.exam.id])).data
        self.assertEqual(detail["attempts"][0]["status"], ExamStatus.COMPLETED)

    def test_with_warnings(self):
        aid = self.new_attempt()
        rw = [{"code": "LEAD_PARTIAL_TRACE", "message": "V2 parcial.", "lead": "V2"}]
        iw = [{"code": "MEASUREMENT_UNRELIABLE", "message": "QT pouco confiável."}]
        self.responses = {"read": lambda rid: read_ok(rid, "OK_WITH_WARNINGS", rw),
                          "interpret": lambda rid: interp_ok(rid, "OK_WITH_WARNINGS", iw)}
        data = self.post("attempt-read", aid).data
        self.assertEqual(data["reading"]["status"], "OK_WITH_WARNINGS")
        self.assertEqual(data["reading"]["warnings"], rw)
        data = self.post("attempt-accept", aid).data
        self.assertEqual(data["status"], ExamStatus.COMPLETED)
        self.assertEqual(data["interpretation"]["warnings"], iw)

    def test_double_read_is_409(self):
        aid = self.new_attempt()
        self.responses = {"read": read_ok}
        self.assertEqual(self.post("attempt-read", aid).status_code, 202)
        resp = self.post("attempt-read", aid)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "invalid_state")
        self.assertEqual(len(self.calls), 1)

    def test_accept_and_reject_require_awaiting_validation(self):
        aid = self.new_attempt()
        for name in ("attempt-accept", "attempt-reject", "attempt-retry"):
            self.assertEqual(self.post(name, aid).status_code, 409, name)


class RefusalAndRejectionTest(FlowTestCase):
    def test_refused_asks_new_photo(self):
        aid = self.new_attempt()
        self.responses = {"read": lambda rid: error(
            "read", rid, "READ_REFUSED_BLURRY", False, category="refused", status="REFUSED",
            details={"region_px": [1, 2, 3, 4],
                     "preview": {"bucket": RESULTS, "key": f"reading/{rid}/r/refusal_preview.png"}})}
        data = self.post("attempt-read", aid).data
        self.assertEqual(data["status"], ExamStatus.READING_REFUSED)
        self.assertEqual(data["reading"]["error"]["code"], "READ_REFUSED_BLURRY")
        self.assertEqual(data["reading"]["refusal_preview_url"], f"https://signed/reading/{aid}/r/refusal_preview.png")
        self.assertEqual(data["reading"]["refusal_region_px"], [1, 2, 3, 4])
        self.assertEqual((data["can_retry"], data["needs_new_photo"]), (False, True))
        self.assertEqual(self.post("attempt-retry", aid).status_code, 409)

    def test_reject_then_new_attempt_resets_exam(self):
        aid = self.new_attempt()
        self.responses = {"read": read_ok}
        self.post("attempt-read", aid)
        resp = self.post("attempt-reject", aid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], ExamStatus.USER_REJECTED)
        self.assertTrue(resp.data["needs_new_photo"])
        self.assertEqual(self.exam_status(), ExamStatus.USER_REJECTED)
        self.assertEqual(self.post("attempt-reject", aid).status_code, 409)

        self.new_attempt()
        self.assertEqual(self.exam_status(), ExamStatus.CREATED)
        self.assertEqual(self.exam.attempts.count(), 2)


class ErrorAndRetryTest(FlowTestCase):
    def test_retryable_read_error_then_retry(self):
        aid = self.new_attempt()
        self.responses = {"read": lambda rid: error("read", rid, "STORAGE_ERROR", True, category="internal")}
        data = self.post("attempt-read", aid).data
        self.assertEqual(data["status"], ExamStatus.READING_ERROR)
        self.assertEqual((data["can_retry"], data["needs_new_photo"]), (True, False))

        self.responses = {"read": read_ok}
        resp = self.post("attempt-retry", aid)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["status"], ExamStatus.AWAITING_VALIDATION)
        self.assertIsNone(resp.data["reading"]["error"])
        self.assertEqual(len(self.calls), 2)

    def test_non_retryable_error_retry_is_409(self):
        aid = self.new_attempt()
        self.responses = {"read": lambda rid: error("read", rid, "READ_GRID_NOT_FOUND", False)}
        data = self.post("attempt-read", aid).data
        self.assertEqual(data["status"], ExamStatus.READING_ERROR)
        self.assertEqual((data["can_retry"], data["needs_new_photo"]), (False, True))
        self.assertEqual(self.post("attempt-retry", aid).status_code, 409)
        self.assertEqual(len(self.calls), 1)

    def test_interpretation_error_then_retry(self):
        aid = self.new_attempt()
        self.responses = {"read": read_ok,
                          "interpret": lambda rid: error("interpret", rid, "INTERNAL_ERROR", True, category="internal")}
        self.post("attempt-read", aid)
        data = self.post("attempt-accept", aid).data
        self.assertEqual(data["status"], ExamStatus.INTERPRETATION_ERROR)
        self.assertTrue(data["can_retry"])
        self.assertEqual(data["interpretation"]["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(data["reading"]["status"], "OK")  # a leitura é preservada

        self.responses["interpret"] = interp_ok
        resp = self.post("attempt-retry", aid)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["status"], ExamStatus.COMPLETED)
        self.assertEqual([c[0] for c in self.calls], ["read", "interpret", "interpret"])

    def test_unexpected_exception_marks_internal_error(self):
        aid = self.new_attempt()
        self.responses = {"read": lambda rid: 1 / 0}
        with self.assertLogs("exams.tasks", "ERROR"):
            data = self.post("attempt-read", aid).data
        self.assertEqual(data["status"], ExamStatus.READING_ERROR)
        self.assertEqual(data["reading"]["error"]["code"], "INTERNAL_ERROR")
        self.assertTrue(data["can_retry"])

    @override_settings(STALE_PROCESSING_SECONDS=600)
    def test_task_timeout(self):
        aid = self.new_attempt()
        old = timezone.now() - timedelta(seconds=601)
        ExamAttempt.objects.filter(id=aid).update(status=ExamStatus.READING, updated_at=old)
        with self.assertLogs("exams.services", "WARNING"):
            data = self.get_attempt(aid)
        self.assertEqual(data["status"], ExamStatus.READING_ERROR)
        self.assertEqual(data["reading"]["error"], {
            "code": "TASK_TIMEOUT", "category": "internal", "retryable": True,
            "message": "O processamento demorou mais que o esperado. Tente de novo."})
        self.assertTrue(data["can_retry"])
        self.assertEqual(self.exam_status(), ExamStatus.READING_ERROR)

    def test_recent_processing_is_not_stale(self):
        aid = self.new_attempt()
        ExamAttempt.objects.filter(id=aid).update(status=ExamStatus.INTERPRETING)
        self.assertEqual(self.get_attempt(aid)["status"], ExamStatus.INTERPRETING)


class IsolationTest(FlowTestCase):
    def test_other_doctor_gets_404(self):
        aid = self.new_attempt()
        other = User.objects.create_user(username="outro", email="o@t.local", password="x", name="O", crm="C2")
        client = APIClient()
        client.force_authenticate(other)
        for name in ("attempt-detail",):
            self.assertEqual(client.get(self.url(name, aid)).status_code, 404)
        for name in ("attempt-read", "attempt-accept", "attempt-reject", "attempt-retry"):
            self.assertEqual(client.post(self.url(name, aid)).status_code, 404, name)
        self.assertEqual(client.get(reverse("exam-detail", args=[self.exam.id])).status_code, 404)
        self.assertEqual(ExamAttempt.objects.get(id=aid).status, ExamStatus.CREATED)

    def test_attempt_of_another_exam_is_404(self):
        aid = self.new_attempt()
        other_exam = Exam.objects.create(patient=self.patient, doctor=self.user)
        self.assertEqual(self.client.get(self.url("attempt-detail", aid, other_exam.id)).status_code, 404)

    def test_requires_token(self):
        aid = self.new_attempt()
        self.assertEqual(APIClient().get(self.url("attempt-detail", aid)).status_code, 401)


class TestScenarioTest(FlowTestCase):
    @override_settings(ECG_TEST_SCENARIOS_ENABLED=True)
    def test_staff_scenarios_reach_lambdas(self):
        self.user.is_staff = True
        self.user.save()
        aid = self.new_attempt(test_scenario={"read": {"scenario": "success"},
                                              "interpret": {"scenario": "failure", "code": "INTERP_NO_QRS_DETECTED"}})
        self.responses = {"read": read_ok, "interpret": interp_ok}
        self.post("attempt-read", aid)
        self.post("attempt-accept", aid)
        self.assertEqual(self.calls[0][1]["mock"], {"scenario": "success"})
        self.assertEqual(self.calls[1][1]["mock"], {"scenario": "failure", "code": "INTERP_NO_QRS_DETECTED"})


class ConfigHealthLoginTest(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="medica", email="m@t.local", password="senha-123",
                                             name="Médica", crm="CRM-1")

    def _config(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get(reverse("config"))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def test_config_shape(self):
        data = self._config()
        self.assertEqual(data["layouts"], ["3x4+1", "12x1"])
        self.assertEqual(data["calibration"], {"speed_mm_s": [12.5, 25, 50], "gain_mm_mv": [5, 10, 20],
                                               "default": {"speed_mm_s": 25, "gain_mm_mv": 10}})

    def test_config_scenarios_flag(self):
        for flag, staff, expected in [(False, False, False), (False, True, False),
                                      (True, False, False), (True, True, True)]:
            with self.subTest(flag=flag, staff=staff), override_settings(ECG_TEST_SCENARIOS_ENABLED=flag):
                self.user.is_staff = staff
                self.user.save()
                self.assertIs(self._config()["test_scenarios_enabled"], expected)

    def test_config_requires_token(self):
        self.assertEqual(self.client.get(reverse("config")).status_code, 401)

    @override_settings(APP_VERSION="abc1234")
    def test_health_without_token(self):
        resp = self.client.get(reverse("health"), HTTP_AUTHORIZATION="Token invalido")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"status": "ok", "db": "ok", "version": "abc1234"})

    def test_health_db_down(self):
        with mock.patch("exams.views.connection.cursor", side_effect=Exception("db down")):
            with self.assertLogs("exams.views", "ERROR"):
                resp = self.client.get(reverse("health"))
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["db"], "error")

    def test_login_returns_is_staff(self):
        self.user.is_staff = True
        self.user.save()
        resp = self.client.post(reverse("login"), {"username": "medica", "password": "senha-123"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.data["user"]["is_staff"], True)
        self.assertEqual(resp.data["user"]["username"], "medica")


@override_settings(ECG_TASK_MODE="sqs", ECG_TASK_QUEUE_URL="https://sqs.sa-east-1.amazonaws.com/1/ecg-tasks")
class SqsDispatchTest(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.sqs = mock.Mock()
        patcher = mock.patch.object(tasks, "_sqs_client", return_value=self.sqs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_publishes_message_and_stays_processing(self):
        aid = self.new_attempt()
        resp = self.post("attempt-read", aid)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["status"], ExamStatus.READING)
        self.sqs.send_message.assert_called_once_with(
            QueueUrl="https://sqs.sa-east-1.amazonaws.com/1/ecg-tasks",
            MessageBody=json.dumps({"ecg_task": "read", "attempt_id": aid}),
        )
        self.assertEqual(self.calls, [])

        # O worker consome: roda uma vez; evento duplicado é ignorado.
        self.responses = {"read": read_ok}
        self.assertTrue(tasks.run_task("read", aid))
        self.assertFalse(tasks.run_task("read", aid))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(ExamAttempt.objects.get(id=aid).status, ExamStatus.AWAITING_VALIDATION)

    def test_publish_failure_marks_retryable_error(self):
        aid = self.new_attempt()
        self.sqs.send_message.side_effect = Exception("AccessDenied")
        with self.assertLogs("exams.tasks", "ERROR"):
            resp = self.post("attempt-read", aid)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["status"], ExamStatus.READING_ERROR)
        self.assertEqual(resp.data["reading"]["error"]["code"], "INTERNAL_ERROR")
        self.assertTrue(resp.data["can_retry"])

    def test_run_task_ignores_unknown_inputs(self):
        self.assertFalse(tasks.run_task("bogus", "x"))
        self.assertFalse(tasks.run_task("read", "00000000-0000-0000-0000-000000000000"))
        self.assertFalse(tasks.run_task("read", "nao-e-uuid"))
