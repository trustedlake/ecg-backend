"""Testes do cliente do contrato v1 (exams/services.py).

O cliente boto3 da lambda e a leitura do sinal no S3 são mockados; as fixtures seguem os
exemplos do CONTRATO_LAMBDAS.md (§1.1, §2.3, §2.4, §3.2). Nenhum teste fala com a AWS.
"""

import io
import json
from unittest import mock

from botocore.exceptions import EndpointConnectionError, ReadTimeoutError
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from . import services
from .models import Exam, ExamAttempt, ExamStatus, Patient, Report

User = get_user_model()

RESULTS = settings.AWS_RESULTS_BUCKET


def _envelope(operation, request_id, status, result=None, error=None, warnings=None):
    return {
        "contract_version": "1.0", "operation": operation, "request_id": request_id,
        "status": status, "warnings": warnings or [], "error": error, "result": result,
        "meta": {"engine": f"ecg-{operation}-mock", "engine_version": "abc123", "duration_ms": 10},
    }


def _read_result(request_id, prefix=None, bucket=RESULTS):
    prefix = prefix or f"reading/{request_id}/run1"
    return {
        "layout": "3x4+1", "calibration": {"speed_mm_s": 25.0, "gain_mm_mv": 10.0},
        "sampling_rate_hz": 500, "units": "mV",
        "grid": {"small_box_px": 5.888, "large_box_px": 29.44, "origin_px": [64.0, 60.0],
                 "rotation_deg": 1.8, "paper_bbox_px": [64.0, 60.0, 1536.0, 960.0]},
        "leads": [
            {"name": "I", "kind": "panel", "row": 0, "col": 0, "start_s": 0.0, "duration_s": 2.5,
             "quality": "good", "coverage": 1.0, "bbox_px": [64, 60, 432, 285], "baseline_y_px": 172.5,
             "samples": [0.0, 0.0012, 0.0031]},
            {"name": "V2", "kind": "panel", "row": 1, "col": 2, "start_s": 5.0, "duration_s": 2.5,
             "quality": "partial", "coverage": 0.6, "samples": [None, 0.1, None]},
        ],
        "artifacts": {
            "overlay": {"bucket": bucket, "key": f"{prefix}/overlay.png", "content_type": "image/png"},
            "signal": {"bucket": bucket, "key": f"{prefix}/signal.json", "content_type": "application/json"},
        },
    }


INTERP_RESULT = {
    "patient": {"sex": "F"},
    "summary": {"classification": "normal", "headline": "ECG dentro dos limites da normalidade",
                "rhythm": "Ritmo sinusal", "heart_rate_bpm": 72,
                "key_findings": ["Ritmo sinusal"], "text": "Ritmo sinusal, FC 72 bpm."},
    "details": {"measurements": {"heart_rate_bpm": {"value": 72, "unit": "bpm", "confidence": "high"}},
                "findings": [], "criteria": [], "per_lead": []},
    "disclaimer": "Resultado gerado automaticamente. Não substitui o laudo de um médico.",
}

SIGNAL = {"sampling_rate_hz": 500, "units": "mV", "leads": [{"name": "II", "samples": [0.0, 0.1]}]}


def _lambda_response(body, function_error=None):
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    if isinstance(raw, str):
        raw = raw.encode()
    response = {"StatusCode": 200, "Payload": io.BytesIO(raw)}
    if function_error:
        response["FunctionError"] = function_error
    return response


class ServicesTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="medico", email="m@test.local", password="x", name="Médica", crm="CRM-1"
        )
        self.patient = Patient.objects.create(doctor=self.user, name="Ana", age=30, sex="F")
        self.exam = Exam.objects.create(patient=self.patient, doctor=self.user)
        self.attempt = ExamAttempt.objects.create(
            exam=self.exam, status=ExamStatus.LEITURA_PROCESSANDO, layout="3x4+1",
            imagem_s3_key=f"u/1/{self.exam.id}/a/original.jpg",
            calibracao={"speed_mm_s": 25, "gain_mm_mv": 10},
        )
        self.rid = str(self.attempt.id)
        self.client_mock = mock.Mock()
        patcher = mock.patch.object(services, "_lambda_client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        presign = mock.patch.object(
            services, "presign_get_url", side_effect=lambda b, k, e: f"https://signed/{b}/{k}"
        )
        presign.start()
        self.addCleanup(presign.stop)

    def _lambda_returns(self, body, function_error=None):
        self.client_mock.invoke.return_value = _lambda_response(body, function_error)

    def _sent_payload(self, call_index=-1):
        return json.loads(self.client_mock.invoke.call_args_list[call_index].kwargs["Payload"])

    def _reload(self):
        self.attempt.refresh_from_db()
        self.exam.refresh_from_db()


class InvokeContractTest(ServicesTestCase):
    def test_valid_envelope_is_returned(self):
        body = _envelope("read", self.rid, "OK", result=_read_result(self.rid))
        self._lambda_returns(body)
        env = services.invoke_contract("ecg-reading-mock", {"request_id": self.rid}, "read")
        self.assertEqual(env["status"], "OK")
        kwargs = self.client_mock.invoke.call_args.kwargs
        self.assertEqual(kwargs["FunctionName"], "ecg-reading-mock")
        self.assertEqual(kwargs["InvocationType"], "RequestResponse")

    def _assert_synthetic(self, env):
        self.assertEqual(env["status"], "ERROR")
        self.assertEqual(env["error"]["code"], "INTERNAL_ERROR")
        self.assertTrue(env["error"]["retryable"])
        self.assertTrue(env["meta"]["synthetic"])

    def test_function_error_becomes_internal_error(self):
        self._lambda_returns({"errorMessage": "Task timed out after 240 s"}, function_error="Unhandled")
        with self.assertLogs("exams.services", "ERROR"):
            env = services.invoke_contract("f", {"request_id": self.rid}, "read")
        self._assert_synthetic(env)

    def test_network_exception_becomes_internal_error(self):
        self.client_mock.invoke.side_effect = EndpointConnectionError(endpoint_url="https://lambda")
        with self.assertLogs("exams.services", "ERROR"):
            env = services.invoke_contract("f", {"request_id": self.rid}, "read")
        self._assert_synthetic(env)

    def test_read_timeout_becomes_internal_error(self):
        self.client_mock.invoke.side_effect = ReadTimeoutError(endpoint_url="https://lambda")
        with self.assertLogs("exams.services", "ERROR"):
            env = services.invoke_contract("f", {"request_id": self.rid}, "read")
        self._assert_synthetic(env)

    def test_malformed_envelopes_become_internal_error(self):
        cases = [
            b"not json",
            [],
            {"status": "MAYBE"},
            _envelope("read", self.rid, "OK", result=None),
            _envelope("read", self.rid, "ERROR", error={"code": "X"}),
            _envelope("read", self.rid, "OK", result={}, warnings=[{"message": "sem code"}]),
            _envelope("read", "outro-id", "OK", result={}),
            _envelope("interpret", self.rid, "REFUSED",
                      error={"code": "X", "category": "refused", "retryable": False, "message": "m"}),
        ]
        for body in cases:
            with self.subTest(body=body):
                self._lambda_returns(body)
                op = "interpret" if isinstance(body, dict) and body.get("operation") == "interpret" else "read"
                with self.assertLogs("exams.services", "ERROR"):
                    env = services.invoke_contract("f", {"request_id": self.rid}, op)
                self._assert_synthetic(env)


class LambdaClientConfigTest(TestCase):
    def test_lambda_client_never_retries_and_waits_read_timeout(self):
        with mock.patch.object(services, "_session") as session:
            services._lambda_client()
        config = session.return_value.client.call_args.kwargs["config"]
        self.assertEqual(config.retries, {"max_attempts": 0})
        self.assertEqual(config.read_timeout, settings.AWS_LAMBDA_READ_TIMEOUT)


class RunReadingTest(ServicesTestCase):
    def test_ok_goes_to_awaiting_validation_without_samples(self):
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        status = services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(status, ExamStatus.AGUARDANDO_VALIDACAO)
        self.assertEqual(self.attempt.status, ExamStatus.AGUARDANDO_VALIDACAO)
        self.assertEqual(self.exam.status, ExamStatus.AGUARDANDO_VALIDACAO)
        reading = self.attempt.reading
        self.assertEqual(reading["status"], "OK")
        self.assertIsNone(reading["error"])
        self.assertEqual(reading["leads"][1], {"name": "V2", "kind": "panel", "quality": "partial", "coverage": 0.6})
        self.assertNotIn("samples", json.dumps(reading))
        self.assertEqual(reading["artifacts"]["signal"]["key"], f"reading/{self.rid}/run1/signal.json")

    def test_payload_follows_contract_without_mock(self):
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        services.run_reading(self.attempt)
        payload = self._sent_payload()
        self.assertEqual(payload["request_id"], self.rid)
        self.assertEqual(payload["image"], {"bucket": settings.AWS_IMAGES_BUCKET, "key": self.attempt.imagem_s3_key})
        self.assertEqual(payload["layout"], "3x4+1")
        self.assertEqual(payload["calibration"], {"speed_mm_s": 25, "gain_mm_mv": 10})
        self.assertNotIn("mock", payload)
        self.assertEqual(self.client_mock.invoke.call_args.kwargs["FunctionName"], settings.AWS_LAMBDA_ECG_READING)

    def test_mock_sent_only_for_read_scenario(self):
        self.attempt.test_scenario = {"interpret": {"scenario": "failure"}}
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        services.run_reading(self.attempt)
        self.assertNotIn("mock", self._sent_payload())

        self.attempt.test_scenario = {"read": {"scenario": "refused", "code": "READ_REFUSED_BLURRY"}}
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        services.run_reading(self.attempt)
        self.assertEqual(self._sent_payload()["mock"], {"scenario": "refused", "code": "READ_REFUSED_BLURRY"})

    def test_ok_with_warnings_keeps_warnings(self):
        warnings = [{"code": "LEAD_PARTIAL_TRACE", "message": "Traçado parcial.", "lead": "V2"},
                    {"code": "IMAGE_ROTATION_CORRECTED", "message": "Alinhada."}]
        self._lambda_returns(_envelope("read", self.rid, "OK_WITH_WARNINGS",
                                       result=_read_result(self.rid), warnings=warnings))
        services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.AGUARDANDO_VALIDACAO)
        self.assertEqual(self.attempt.reading["warnings"], warnings)

    def test_refused_keeps_preview_and_region(self):
        error = {"code": "READ_REFUSED_OCCLUDED", "category": "refused", "retryable": False,
                 "message": "Há sobreposição sobre o traçado.",
                 "details": {"region_px": [560, 300, 960, 700],
                             "preview": {"bucket": RESULTS, "key": f"reading/{self.rid}/r/refusal_preview.png"}}}
        self._lambda_returns(_envelope("read", self.rid, "REFUSED", error=error))
        services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.LEITURA_RECUSADA)
        self.assertEqual(self.exam.status, ExamStatus.LEITURA_RECUSADA)
        reading = self.attempt.reading
        self.assertEqual(reading["error"], {"code": "READ_REFUSED_OCCLUDED", "category": "refused",
                                            "retryable": False, "message": "Há sobreposição sobre o traçado."})
        self.assertEqual(reading["refusal_region_px"], [560, 300, 960, 700])
        self.assertEqual(reading["artifacts"]["refusal_preview"]["key"], f"reading/{self.rid}/r/refusal_preview.png")
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": False, "needs_new_photo": True})

    def test_refused_preview_outside_prefix_is_dropped(self):
        error = {"code": "READ_REFUSED_BLURRY", "category": "refused", "retryable": False, "message": "Borrada.",
                 "details": {"preview": {"bucket": RESULTS, "key": "reading/outra-tentativa/r/p.png"}}}
        self._lambda_returns(_envelope("read", self.rid, "REFUSED", error=error))
        services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.LEITURA_RECUSADA)
        self.assertIsNone(self.attempt.reading["artifacts"]["refusal_preview"])
        self.assertIsNone(self.attempt.reading["refusal_region_px"])

    def test_error_not_retryable(self):
        error = {"code": "READ_GRID_NOT_FOUND", "category": "processing", "retryable": False,
                 "message": "Não foi possível detectar a grade."}
        self._lambda_returns(_envelope("read", self.rid, "ERROR", error=error))
        services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.ERRO_LEITURA)
        self.assertEqual(self.attempt.reading["error"]["code"], "READ_GRID_NOT_FOUND")
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": False, "needs_new_photo": True})

    def test_error_retryable(self):
        error = {"code": "STORAGE_ERROR", "category": "internal", "retryable": True, "message": "Falha no S3."}
        self._lambda_returns(_envelope("read", self.rid, "ERROR", error=error))
        services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.ERRO_LEITURA)
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": True, "needs_new_photo": False})

    def test_function_error_marks_retryable_read_error(self):
        self._lambda_returns({"errorMessage": "boom"}, function_error="Unhandled")
        with self.assertLogs("exams.services", "ERROR"):
            services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.ERRO_LEITURA)
        self.assertEqual(self.attempt.reading["error"]["code"], "INTERNAL_ERROR")
        self.assertTrue(services.attempt_flags(self.attempt)["can_retry"])

    def test_artifacts_outside_bucket_or_prefix_become_internal_error(self):
        cases = [
            _read_result(self.rid, bucket="outro-bucket"),
            _read_result(self.rid, prefix="reading/outra-tentativa/run1"),
            _read_result(self.rid, prefix=f"reading/{self.rid}/../x"),
            {**_read_result(self.rid), "artifacts": {"overlay": None}},
        ]
        for result in cases:
            with self.subTest(artifacts=result["artifacts"]):
                self._lambda_returns(_envelope("read", self.rid, "OK", result=result))
                with self.assertLogs("exams.services", "ERROR"):
                    services.run_reading(self.attempt)
                self._reload()
                self.assertEqual(self.attempt.status, ExamStatus.ERRO_LEITURA)
                self.assertEqual(self.attempt.reading["error"]["code"], "INTERNAL_ERROR")
                self.assertIsNone(self.attempt.reading["artifacts"]["signal"])

    def test_exam_mirrors_only_latest_attempt(self):
        newer = ExamAttempt.objects.create(exam=self.exam, status=ExamStatus.CRIADO, imagem_s3_key="k2")
        Exam.objects.filter(id=self.exam.id).update(status=ExamStatus.CRIADO)
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        services.run_reading(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.AGUARDANDO_VALIDACAO)
        self.assertEqual(self.exam.status, ExamStatus.CRIADO)
        self.assertEqual(newer.status, ExamStatus.CRIADO)


class RunInterpretationTest(ServicesTestCase):
    def setUp(self):
        super().setUp()
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        services.run_reading(self.attempt)
        self.attempt.status = ExamStatus.INTERPRETACAO_PROCESSANDO
        self.attempt.save()
        self.client_mock.reset_mock()
        s3 = mock.patch.object(services, "_read_json_from_s3", return_value=SIGNAL)
        self.s3_read = s3.start()
        self.addCleanup(s3.stop)

    def test_ok_creates_report_and_concludes(self):
        self._lambda_returns(_envelope("interpret", self.rid, "OK", result=INTERP_RESULT))
        status = services.run_interpretation(self.attempt)
        self._reload()
        self.assertEqual(status, ExamStatus.CONCLUIDO)
        self.assertEqual(self.exam.status, ExamStatus.CONCLUIDO)
        report = Report.objects.get(attempt=self.attempt)
        self.assertEqual(report.classification, "normal")
        self.assertEqual(report.summary["heart_rate_bpm"], 72)
        self.assertEqual(report.engine, "ecg-interpret-mock")
        self.assertEqual(report.engine_version, "abc123")
        self.assertEqual(self.attempt.interpretation["status"], "OK")
        self.s3_read.assert_called_once_with(RESULTS, f"reading/{self.rid}/run1/signal.json")
        payload = self._sent_payload()
        self.assertEqual(payload["signal"], SIGNAL)
        self.assertEqual(payload["patient"], {"sex": "F"})
        self.assertNotIn("mock", payload)
        self.assertEqual(self.client_mock.invoke.call_args.kwargs["FunctionName"],
                         settings.AWS_LAMBDA_ECG_INTERPRETATION)

        view = services.attempt_interpretation_view(self.attempt)
        self.assertEqual(view["summary"]["classification"], "normal")
        self.assertEqual(view["disclaimer"], INTERP_RESULT["disclaimer"])
        self.assertIsNone(view["error"])
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": False, "needs_new_photo": False})

    def test_male_sex_and_mock_reach_payload(self):
        Patient.objects.filter(id=self.patient.id).update(sex="M")
        self.attempt.test_scenario = {"interpret": {"scenario": "success_with_warnings"}}
        self.attempt.save()
        attempt = ExamAttempt.objects.get(id=self.attempt.id)
        self._lambda_returns(_envelope("interpret", self.rid, "OK", result=INTERP_RESULT))
        services.run_interpretation(attempt)
        payload = self._sent_payload()
        self.assertEqual(payload["patient"], {"sex": "M"})
        self.assertEqual(payload["mock"], {"scenario": "success_with_warnings"})

    def test_ok_with_warnings_goes_to_report(self):
        warnings = [{"code": "LEAD_LOW_QUALITY", "message": "V2 baixa.", "lead": "V2"}]
        self._lambda_returns(_envelope("interpret", self.rid, "OK_WITH_WARNINGS",
                                       result=INTERP_RESULT, warnings=warnings))
        services.run_interpretation(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.CONCLUIDO)
        self.assertEqual(Report.objects.get(attempt=self.attempt).warnings, warnings)
        self.assertEqual(services.attempt_interpretation_view(self.attempt)["warnings"], warnings)

    def test_error(self):
        error = {"code": "INTERP_NO_QRS_DETECTED", "category": "processing", "retryable": False,
                 "message": "Nenhum complexo QRS."}
        self._lambda_returns(_envelope("interpret", self.rid, "ERROR", error=error))
        services.run_interpretation(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.status, ExamStatus.ERRO_INTERPRETACAO)
        self.assertEqual(self.exam.status, ExamStatus.ERRO_INTERPRETACAO)
        self.assertFalse(Report.objects.filter(attempt=self.attempt).exists())
        self.assertEqual(self.attempt.interpretation["error"]["code"], "INTERP_NO_QRS_DETECTED")
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": False, "needs_new_photo": True})
        view = services.attempt_interpretation_view(self.attempt)
        self.assertIsNone(view["summary"])

    def test_missing_sex_does_not_invoke(self):
        Patient.objects.filter(id=self.patient.id).update(sex=None)
        attempt = ExamAttempt.objects.get(id=self.attempt.id)
        services.run_interpretation(attempt)
        attempt.refresh_from_db()
        self.client_mock.invoke.assert_not_called()
        self.assertEqual(attempt.status, ExamStatus.ERRO_INTERPRETACAO)
        self.assertEqual(attempt.interpretation["error"]["code"], "SEX_MISSING")
        self.assertEqual(services.attempt_flags(attempt), {"can_retry": False, "needs_new_photo": True})

    def test_signal_download_failure_is_retryable_storage_error(self):
        self.s3_read.side_effect = Exception("NoSuchKey")
        with self.assertLogs("exams.services", "ERROR"):
            services.run_interpretation(self.attempt)
        self._reload()
        self.client_mock.invoke.assert_not_called()
        self.assertEqual(self.attempt.status, ExamStatus.ERRO_INTERPRETACAO)
        self.assertEqual(self.attempt.interpretation["error"]["code"], "STORAGE_ERROR")
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": True, "needs_new_photo": False})

    def test_function_error_is_retryable(self):
        self._lambda_returns({"errorMessage": "OOM"}, function_error="Unhandled")
        with self.assertLogs("exams.services", "ERROR"):
            services.run_interpretation(self.attempt)
        self._reload()
        self.assertEqual(self.attempt.interpretation["error"]["code"], "INTERNAL_ERROR")
        self.assertTrue(services.attempt_flags(self.attempt)["can_retry"])

    def test_requires_successful_reading(self):
        self.attempt.reading = {"status": "REFUSED", "artifacts": {}}
        with self.assertRaises(ValueError):
            services.run_interpretation(self.attempt)
        self.client_mock.invoke.assert_not_called()


class AttemptViewsTest(ServicesTestCase):
    def test_no_reading_yet(self):
        self.assertIsNone(services.attempt_reading_view(self.attempt))
        self.assertIsNone(services.attempt_interpretation_view(self.attempt))
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": False, "needs_new_photo": False})

    def test_reading_view_presigns_overlay_and_hides_keys(self):
        self._lambda_returns(_envelope("read", self.rid, "OK", result=_read_result(self.rid)))
        services.run_reading(self.attempt)
        view = services.attempt_reading_view(self.attempt)
        self.assertEqual(set(view), {"status", "warnings", "error", "overlay_url", "refusal_preview_url",
                                     "refusal_region_px", "leads"})
        self.assertEqual(view["overlay_url"], f"https://signed/{RESULTS}/reading/{self.rid}/run1/overlay.png")
        self.assertIsNone(view["refusal_preview_url"])
        self.assertEqual(len(view["leads"]), 2)
        self.assertNotIn("samples", json.dumps(view))
        self.assertNotIn("https://", json.dumps(ExamAttempt.objects.get(id=self.attempt.id).reading))

    def test_flags_for_user_rejection(self):
        self.attempt.status = ExamStatus.REJEITADO_USUARIO
        self.assertEqual(services.attempt_flags(self.attempt), {"can_retry": False, "needs_new_photo": True})
