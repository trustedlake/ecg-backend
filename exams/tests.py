"""Testes da API de exames (lÃ³gica de orquestraÃ§Ã£o + isolamento por mÃ©dico).

As lambdas sÃ£o MOCKADAS em `exams.services.invoke_lambda` e as URLs presigned
em `exams.services.presign_get_url/presign_put_url` â€” rodar contra a AWS de
verdade Ã© coberto por smoke manual (ver README). O teste espelha o mapeamento
campo-a-campo usado em produÃ§Ã£o (mesmos nomes dos handlers).
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Exam, ExamAttempt, ExamStatus, Feedback, Patient, Report

User = get_user_model()

LAMBDA1_OUT = {
    "exame_id": "00000000-0000-0000-0000-000000000001",
    "image_type": "grayscale",
    "image_type_motivo": None,
    "ecg_colors": {"trace": [[1, 2], [3, 4]]},
    "mascara_traco_s3_key": "results/xxx/trace_mask.npz",
    "imagem_original_s3_key": "exam/attempt/original.png",
    "calibracao": {"mm_per_mv": 10.0, "mm_per_second": 25.0},
}

LAMBDA2_OUT = {
    "exame_id": "00000000-0000-0000-0000-000000000001",
    "ecg_layout": "3x4+1",
    "lead_labels": ["I", "II"],
    "rhythm_index": 1,
    "paineis": [{"lead": "I", "y0": 1, "y1": 10, "x0": 1, "x1": 10}],
    "ecg_pintado_traco_s3_key": "results/exam/overlay_trace.png",
    "ecg_pintado_sinal_s3_key": "results/exam/overlay_signal.png",
    "sinais": [{"lead": "I", "amostras": [0.0, 1.0]}],
    "fidelidade_por_derivacao": {"I": {"ok": True, "flags": []}},
    "fidelidade": "bom",
    "calibracao": {"mm_per_mv": 10.0},
}

LAMBDA3_OUT = {
    "exame_id": "00000000-0000-0000-0000-000000000001",
    "ritmo": {"fc_bpm": 72, "ritmo": "sinusal"},
    "eixo_graus": 45.0,
    "sokolow_mm": 12.5,
    "derivacoes": [{"lead": "I"}],
    "indicadores": [{"indicador": "P", "valor": 1}],
    "achados": [],
    "conclusao": "ECG sem alteraÃ§Ãµes detectadas (FC 72 bpm).",
    "confiabilidade": "bom",
    "versao_codigo": "0.1.0",
}


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
            {"filename": "ecg.png"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["upload_url"], "https://presigned/PUT")

        attempt = ExamAttempt.objects.get()
        self.assertTrue(attempt.imagem_s3_key.endswith("original.png"))
        self.assertEqual(attempt.status, ExamStatus.CRIADO)
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
        self._create_url = reverse("attempt-create", args=[self.exam.id])
        self._process_url = reverse("attempt-process", args=[self.exam.id, self.attempt.id])
        self._aprovar_url = reverse("attempt-aprovar", args=[self.exam.id, self.attempt.id])

    @mock.patch("exams.services.presign_get_url",
                side_effect=lambda bucket, key, *a: f"http://get/{key}")
    @mock.patch("exams.services.invoke_lambda", side_effect=[LAMBDA1_OUT, LAMBDA2_OUT])
    def test_phase_a_maps_fields(self, _l, _g):
        resp = self.client.post(self._process_url)
        self.assertEqual(resp.status_code, 200)

        self.attempt.refresh_from_db()
        self.exam.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamStatus.AGUARDANDO_APROVACAO)
        self.assertEqual(self.exam.status, ExamStatus.AGUARDANDO_APROVACAO)
        self.assertEqual(self.attempt.image_type, "grayscale")
        self.assertEqual(self.attempt.mascara_s3_key, LAMBDA1_OUT["mascara_traco_s3_key"])
        self.assertEqual(self.attempt.overlay_trace_s3_key, "results/exam/overlay_trace.png")
        self.assertEqual(self.attempt.overlay_signal_s3_key, "results/exam/overlay_signal.png")
        self.assertEqual(self.attempt.sinais, LAMBDA2_OUT["sinais"])
        self.assertEqual(self.attempt.fidelidade, "bom")
        self.assertEqual(self.attempt.rhythm_index, 1)
        self.assertEqual(resp.data["fidelidade"], "bom")
        self.assertEqual(resp.data["status"], ExamStatus.AGUARDANDO_APROVACAO)
        self.assertTrue(resp.data["overlay_trace_url"].startswith("http"))

    @mock.patch("exams.services.invoke_lambda", side_effect=Exception("boom"))
    def test_process_marks_erro_on_failure(self, _m):
        resp = self.client.post(self._process_url)
        self.assertEqual(resp.status_code, 502)
        self.attempt.refresh_from_db()
        self.exam.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamStatus.ERRO)
        self.assertEqual(self.exam.status, ExamStatus.ERRO)

    def test_process_wrong_state_conflict(self):
        self.attempt.status = ExamStatus.REJEITADO
        self.attempt.save()
        resp = self.client.post(self._process_url)
        self.assertEqual(resp.status_code, 409)

    @mock.patch("exams.services.presign_get_url",
                    side_effect=lambda bucket, key, *a: f"http://get/{key}")
    @mock.patch("exams.services.invoke_lambda", return_value=LAMBDA3_OUT)
    def test_approve_creates_report_and_concludes(self, _mock, _g):
        self.attempt.status = ExamStatus.AGUARDANDO_APROVACAO
        self.attempt.sinais = LAMBDA2_OUT["sinais"]
        self.attempt.lead_labels = ["I", "II"]
        self.attempt.calibracao = {}
        self.attempt.fidelidade = "bom"
        self.attempt.save()

        resp = self.client.post(self._aprovar_url)
        self.assertEqual(resp.status_code, 200)
        report = Report.objects.get(attempt=self.attempt)
        self.assertEqual(report.eixo_graus, 45.0)
        self.assertEqual(report.confiabilidade, "bom")
        self.assertEqual(report.versao_codigo, "0.1.0")

        self.attempt.refresh_from_db()
        self.exam.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamStatus.CONCLUIDO)
        self.assertEqual(self.exam.status, ExamStatus.CONCLUIDO)
        self.assertEqual(resp.data["ritmo"]["fc_bpm"], 72)

    def test_approve_wrong_state(self):
        self.attempt.status = ExamStatus.CRIADO
        self.attempt.save()
        resp = self.client.post(self._aprovar_url)
        self.assertEqual(resp.status_code, 409)

    def test_reject(self):
        self.attempt.status = ExamStatus.AGUARDANDO_APROVACAO
        self.attempt.save()
        resp = self.client.post(reverse("attempt-rejeitar", args=[self.exam.id, self.attempt.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], ExamStatus.REJEITADO)


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
