"""Testes da API de exames (lÃ³gica de orquestraÃ§Ã£o + isolamento por mÃ©dico).

As lambdas sÃ£o MOCKADAS em `exams.services.invoke_lambda` e as URLs presigned
em `exams.services.presign_get_url/presign_put_url` â€” rodar contra a AWS de
verdade Ã© coberto por smoke manual (ver README). O teste espelha o mapeamento
campo-a-campo usado em produÃ§Ã£o (mesmos nomes dos handlers).
"""

import unittest
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Exam, ExamAttempt, ExamStatus, Feedback, Patient, Report

User = get_user_model()

LAMBDA_ANALYZE_OUT = {
    "status": "CONSISTENCY_PASS", "warnings": [],
    "leads": [{"name":"I"},{"name":"II"}],
    "grid": {"valid":True},
    "artifacts": {
        "result":"ecg-analyser/{A}/initial/result.json",
        "signals":"ecg-analyser/{A}/initial/signals.npz",
        "trace_masks":"ecg-analyser/{A}/initial/trace_masks.npz",
        "visao_geral":"ecg-analyser/{A}/initial/visao_geral.png",
        "working_image":"ecg-analyser/{A}/initial/working_image.png",
    },
}

LAMBDA_REJECTED_OUT = {
    "status":"REJECTED_LOW_TRACE_GRID_CONTRAST", "warnings":["low contrast"],
    "leads":[], "artifacts":{"result":"ecg-analyser/{A}/initial/result.json"},
}

LAMBDA_REFINE_OUT = {
    "status":"MANUAL_REVIEW_REQUIRED", "warnings":["MANUAL_CORRECTION_UNVERIFIED"],
    "manual_correction":{"lead":"II","changed_columns":2},
    "artifacts":{
        "result":"ecg-analyser/{A}/r2/result.json",
        "signals":"ecg-analyser/{A}/r2/signals.npz",
        "trace_masks":"ecg-analyser/{A}/r2/trace_masks.npz",
        "working_image":"ecg-analyser/{A}/r2/working_image.png",
        "correction_preview":"ecg-analyser/{A}/r2/correction_preview.png",
    },
}


def _file(name, ctype="application/json"):
    return {"bucket": "res", "key": "reading/{A}/run/m/" + name, "content_type": ctype}


def _method(name, status, score):
    ok = status == "OK"
    return {"method": name, "version": "v", "status": status, "reason": None if ok else "x",
            "selector_score": score if ok else None, "sampling_rate_hz": 200.0,
            "files": {"json": _file("result.json"), "image": _file("preview.png", "image/png"),
                      "leads": _file("leads.json")}}


LAMBDA_READ_OUT = {
    "contract_version": "2.0", "operation": "read", "status": "OK_WITH_WARNINGS",
    "warnings": [{"code": "METHODS_PARTIAL_RESULT", "message": "m"}], "error": None,
    "result": {"methods": [_method("a", "OK", 60.0), _method("b", "OK", 90.0), _method("c", "BLOCKED", None)]},
}

LAMBDA_READ_REFUSED = {
    "contract_version": "2.0", "operation": "read", "status": "REFUSED", "warnings": [],
    "error": {"code": "READ_REFUSED_LOW_CONTRAST", "message": "m"}, "result": None,
}

LEADS_JSON = {"leads": [{"name": "I"}, {"name": "II"}]}


def lambda_fake(**outputs):
    """Fake invoke_lambda: answers per operation, with keys under the real attempt id."""
    import json
    def fake(function_name, payload):
        out = outputs[payload["operation"]]
        return json.loads(json.dumps(out).replace("{A}", payload.get("attempt_id") or payload["request_id"]))
    return fake


def real_keys(artifacts, attempt):
    return {k: v.replace("{A}", str(attempt.id)) for k, v in artifacts.items()}


class BaseAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="medico-teste", email="medico@test.local", password="senha-123", name="MÃ©dica Teste", crm="CRM-1"
        )
        self.other = User.objects.create_user(
            username="outro-medico", email="outro@test.local", password="senha-123", name="Outro MÃ©dico", crm="CRM-2"
        )
        self.client.force_authenticate(user=self.user)

    def _exame(self, doctor=None):
        p = Patient.objects.create(doctor=doctor or self.user, name="Ana", age=30, gender="F")
        return Exam.objects.create(patient=p, doctor=doctor or self.user, reason="rotina")

    def _attempt(self, exam, status_=ExamStatus.CRIADO, **kwargs):
        kwargs.setdefault("calibracao", {"speed_mm_s":25,"gain_mm_mv":10})
        return ExamAttempt.objects.create(
            exam=exam, status=status_,
            imagem_s3_key=f"{exam.id}/a/original.png", **kwargs,
        )


class ExamListCreateTest(BaseAPITestCase):
    def test_create_exam_with_inline_patient(self):
        resp = self.client.post(reverse("exam-list"), {
            "name": "Carlos", "age": 45, "gender": "M", "reason": "dor torÃ¡cica",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Exam.objects.count(), 1)
        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(resp.data["status"], ExamStatus.CRIADO)
        self.assertEqual(resp.data["patient"]["name"], "Carlos")

    def test_create_exam_with_existing_patient(self):
        p = Patient.objects.create(doctor=self.user, name="Bia", age=28, gender="F")
        resp = self.client.post(reverse("exam-list"), {
            "patient_id": str(p.id), "reason": "check-up",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["patient"]["id"], str(p.id))

    def test_create_exam_without_patient_refuses(self):
        resp = self.client.post(reverse("exam-list"), {"reason": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_create_exam_with_other_doctor_patient_refuses(self):
        p = Patient.objects.create(doctor=self.other, name="Lia", age=1, gender="F")
        resp = self.client.post(reverse("exam-list"), {"patient_id": str(p.id)}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_ecg_layout_force_3x4_1(self):
        resp = self.client.post(reverse("exam-list"), {
            "name": "ZÃ©", "age": 50, "gender": "M", "ecg_layout": "6x2+1",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_list_only_own_exams(self):
        for _ in range(3):
            self._exame()
        self._exame(doctor=self.other)
        resp = self.client.get(reverse("exam-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 3)

    def test_list_requires_auth(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(reverse("exam-list"))
        self.assertEqual(resp.status_code, 401)


class AttemptCreateTest(BaseAPITestCase):
    @mock.patch("exams.services.presign_put_url", return_value="https://presigned/PUT")
    def test_creates_attempt_and_upload_url(self, _put):
        exam = self._exame()
        resp = self.client.post(
            reverse("attempt-create", args=[exam.id]),
            {"filename": "ecg.png", "content_type":"image/png", "speed_mm_s":25, "gain_mm_mv":10},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["upload_url"], "https://presigned/PUT")

        attempt = ExamAttempt.objects.get()
        self.assertTrue(attempt.imagem_s3_key.endswith("original.png"))
        self.assertEqual(attempt.status, ExamStatus.CRIADO)
        self.assertEqual(attempt.calibracao["gain_mm_mv"], 10)
        self.assertTrue(resp.data["imagem_s3_key"].endswith("original.png"))

    @mock.patch("exams.services.presign_put_url", return_value="https://presigned/PUT")
    def test_foreign_exam_not_found(self, _put):
        exam = self._exame(doctor=self.other)
        resp = self.client.post(reverse("attempt-create", args=[exam.id]), {}, format="json")
        self.assertEqual(resp.status_code, 404)


class AttemptFlowTest(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.exam = self._exame()
        self.attempt = self._attempt(self.exam)
        self._process_url = reverse("attempt-process", args=[self.exam.id, self.attempt.id])
        self._refine_url = reverse("attempt-refine", args=[self.exam.id, self.attempt.id])
        self._approve_url = reverse("attempt-aprovar", args=[self.exam.id, self.attempt.id])

    @mock.patch("exams.services._read_json", return_value=LEADS_JSON)
    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    @mock.patch("exams.services.invoke_lambda", side_effect=lambda_fake(read=LAMBDA_READ_OUT))
    def test_read_lambda_picks_highest_selector_score(self, invoke, signed, _leads):
        response=self.client.post(self._process_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invoke.call_count, 1)
        payload = invoke.call_args.args[1]
        self.assertEqual(invoke.call_args.args[0], "ecg-reading")
        self.assertEqual(payload["operation"], "read")
        self.assertEqual(payload["layout"], "3x4+1")
        self.assertEqual(payload["calibration"]["speed_mm_s"], 25)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamStatus.AGUARDANDO_APROVACAO)
        self.assertEqual(self.attempt.sinais["analyser_status"], "OK_WITH_WARNINGS")
        self.assertEqual(self.attempt.sinais["chosen_method"], "b")
        self.assertEqual(self.attempt.lead_labels, ["I", "II"])
        self.assertTrue(self.attempt.overlay_trace_s3_key.endswith("preview.png"))

    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    @mock.patch("exams.services.invoke_lambda", side_effect=lambda_fake(read=LAMBDA_READ_REFUSED))
    def test_rejection_does_not_export_signals(self, invoke, signed):
        response=self.client.post(self._process_url)
        self.assertEqual(response.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamStatus.REJEITADO)
        self.assertEqual(self.attempt.sinais["leads"], [])
        self.assertEqual(self.client.post(self._approve_url).status_code, 409)
        refine_body = {"coordinate_system": "normalized_image_v1", "lead": "II", "strokes": [
            {"lead": "II", "mode": "include", "points": [{"x": 0.2, "y": 0.2}]}]}
        self.assertEqual(self.client.post(self._refine_url, refine_body, format="json").status_code, 409)

    @mock.patch("exams.services._read_json", return_value=LEADS_JSON)
    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    @mock.patch("exams.services.invoke_lambda", side_effect=lambda_fake(read=LAMBDA_READ_OUT))
    def test_get_state_does_not_invoke_lambda_twice(self, invoke, signed, _leads):
        self.client.post(self._process_url)
        response=self.client.get(self._process_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"],ExamStatus.AGUARDANDO_APROVACAO)
        self.assertEqual(response.data["result_url"],"https://signed")
        self.assertEqual(invoke.call_count,1)

    @unittest.skip("v1 reading Lambda has no refine operation yet")
    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    @mock.patch("exams.services.invoke_lambda", side_effect=lambda_fake(refine=LAMBDA_REFINE_OUT))
    def test_manual_paint_saves_new_version(self, invoke, signed):
        self.assertEqual(self.client.post(self._process_url).status_code,200)
        response=self.client.post(self._refine_url,{
            "coordinate_system":"normalized_image_v1", "lead":"II", "strokes":[
                {"lead":"II","mode":"exclude","points":[{"x":0.4,"y":0.5}]}]},format="json")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['status'],'MANUAL_REVIEW_REQUIRED')
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.overlay_trace_s3_key, real_keys(LAMBDA_REFINE_OUT['artifacts'], self.attempt)['correction_preview'])
        self.assertEqual(len(self.attempt.sinais['correction_history']),1)
        self.assertEqual(invoke.call_count,2)

    @mock.patch("exams.services.invoke_lambda", side_effect=Exception("boom"))
    def test_upstream_failure_marks_error(self, invoke):
        response=self.client.post(self._process_url)
        self.assertEqual(response.status_code,502)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status,ExamStatus.ERRO)

    def test_reprocess_wrong_state_returns_409(self):
        self.attempt.status=ExamStatus.REJEITADO
        self.attempt.save()
        self.assertEqual(self.client.post(self._process_url).status_code,409)

    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    @mock.patch("exams.services.invoke_lambda")
    def test_approval_only_confirms_extraction_without_diagnosis(self, invoke, signed):
        self.attempt.status=ExamStatus.AGUARDANDO_APROVACAO
        self.attempt.sinais={"analyser_status":"CONSISTENCY_PASS"}
        self.attempt.fidelidade="bom"
        self.attempt.save()
        response=self.client.post(self._approve_url)
        self.assertEqual(response.status_code,200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status,ExamStatus.CONCLUIDO)
        report=Report.objects.get(attempt=self.attempt)
        self.assertIsNone(report.conclusao)
        self.assertEqual(report.versao_codigo,"ecg-analyser-extraction-only")
        invoke.assert_not_called()

    def test_foreign_user_cannot_refine_another_doctors_attempt(self):
        self.attempt.status=ExamStatus.AGUARDANDO_APROVACAO
        self.attempt.save()
        self.client.force_authenticate(user=self.other)
        data={"coordinate_system":"normalized_image_v1", "lead":"II", "strokes":[
            {"lead":"II","mode":"exclude","points":[{"x":0.2,"y":0.2}]}]}
        response=self.client.post(self._refine_url,data,format="json")
        self.assertEqual(response.status_code,404)

    @mock.patch("exams.services.run_refine")
    def test_busy_refine_blocked_before_second_lambda(self, worker):
        self.attempt.status=ExamStatus.FASE_A_PROCESSANDO
        self.attempt.save()
        data={"coordinate_system":"normalized_image_v1", "lead":"II", "strokes":[
            {"lead":"II","mode":"include","points":[{"x":0.2,"y":0.2}]}]}
        response=self.client.post(self._refine_url,data,format="json")
        self.assertEqual(response.status_code,409)
        worker.assert_not_called()

    @mock.patch("exams.services.run_refine")
    def test_refine_failure_restores_review_status(self, worker):
        from exams.services import UpstreamError
        self.attempt.status=ExamStatus.AGUARDANDO_APROVACAO
        self.attempt.save()
        worker.side_effect=UpstreamError("fake network failure")
        data={"coordinate_system":"normalized_image_v1", "lead":"II", "strokes":[
            {"lead":"II","mode":"include","points":[{"x":0.2,"y":0.2}]}]}
        response=self.client.post(self._refine_url,data,format="json")
        self.assertEqual(response.status_code,502)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status,ExamStatus.AGUARDANDO_APROVACAO)

    def test_approval_wrong_state_returns_409(self):
        self.assertEqual(self.client.post(self._approve_url).status_code,409)

    def test_reject_waiting(self):
        self.attempt.status=ExamStatus.AGUARDANDO_APROVACAO
        self.attempt.save()
        response=self.client.post(reverse("attempt-rejeitar",args=[self.exam.id,self.attempt.id]))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data["status"],ExamStatus.REJEITADO)


class FeedbackTest(BaseAPITestCase):
    def test_feedback_created(self):
        attempt = self._attempt(self._exame())
        resp = self.client.post(reverse("attempt-feedback", args=[attempt.id]), {
            "issues": ["traÃ§ado errado"], "description": "QRS girado",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        fb = Feedback.objects.get()
        self.assertEqual(fb.user, self.user)
        self.assertEqual(fb.attempt, attempt)

    def test_feedback_foreign_attempt_404(self):
        attempt = self._attempt(self._exame(doctor=self.other))
        resp = self.client.post(reverse("attempt-feedback", args=[attempt.id]), {"issues": []}, format="json")
        self.assertEqual(resp.status_code, 404)


class StaleProcessingRecoveryTest(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.exam = self._exame()
        self.attempt = self._attempt(self.exam, status_=ExamStatus.FASE_A_PROCESSANDO)
        Exam.objects.filter(pk=self.exam.pk).update(status=ExamStatus.FASE_A_PROCESSANDO)
        self.url = reverse("attempt-process", args=[self.exam.id, self.attempt.id])

    def _age(self, seconds):
        from datetime import timedelta
        from django.utils import timezone
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(updated_at=timezone.now() - timedelta(seconds=seconds))

    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    def test_recent_processing_is_left_alone(self, _signed):
        self._age(30)
        self.assertEqual(self.client.get(self.url).data["status"], ExamStatus.FASE_A_PROCESSANDO)

    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    def test_stale_analysis_becomes_error(self, _signed):
        self._age(100000)
        self.assertEqual(self.client.get(self.url).data["status"], ExamStatus.ERRO)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.status, ExamStatus.ERRO)

    @mock.patch("exams.services.presign_get_url", return_value="https://signed")
    def test_stale_refine_returns_to_review(self, _signed):
        ExamAttempt.objects.filter(pk=self.attempt.pk).update(sinais={"result_s3_key": "ecg-analyser/x/initial/result.json"})
        self._age(100000)
        self.assertEqual(self.client.get(self.url).data["status"], ExamStatus.AGUARDANDO_APROVACAO)
