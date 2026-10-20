"""Testes da API de exames: modelos e validação de entrada do contrato v1.

As presigned URLs são mockadas em `exams.services.presign_put_url`; nenhum teste fala com a AWS.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import Exam, ExamAttempt, ExamStatus, Feedback, Patient

User = get_user_model()

PRESIGN_PUT = "exams.services.presign_put_url"


class BaseAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="medico-teste", email="medico@test.local", password="senha-123", name="Médica Teste", crm="CRM-1"
        )
        self.other = User.objects.create_user(
            username="outro-medico", email="outro@test.local", password="senha-123", name="Outro Médico", crm="CRM-2"
        )
        self.client.force_authenticate(user=self.user)

    def _exame(self, doctor=None):
        p = Patient.objects.create(doctor=doctor or self.user, name="Ana", age=30, sex="F")
        return Exam.objects.create(patient=p, doctor=doctor or self.user, reason="rotina")

    def _attempt(self, exam, status_=ExamStatus.CREATED, **kwargs):
        kwargs.setdefault("calibration", {"speed_mm_s": 25, "gain_mm_mv": 10})
        return ExamAttempt.objects.create(
            exam=exam, status=status_,
            image_s3_key=f"{exam.id}/a/original.png", **kwargs,
        )


class ExamListCreateTest(BaseAPITestCase):
    def test_create_exam_with_inline_patient(self):
        resp = self.client.post(reverse("exam-list"), {
            "name": "Carlos", "age": 45, "sex": "M", "reason": "dor torácica",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], ExamStatus.CREATED)
        self.assertEqual(resp.data["patient"]["name"], "Carlos")
        self.assertEqual(resp.data["patient"]["sex"], "M")
        self.assertEqual(Patient.objects.get().sex, "M")

    def test_create_exam_with_existing_patient(self):
        p = Patient.objects.create(doctor=self.user, name="Bia", age=28, sex="F")
        resp = self.client.post(reverse("exam-list"), {
            "patient_id": str(p.id), "reason": "check-up",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["patient"]["id"], str(p.id))

    def test_reason_is_optional(self):
        resp = self.client.post(reverse("exam-list"), {"name": "Rui", "age": 60, "sex": "M"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["reason"], "")

    def test_create_exam_without_patient_refuses(self):
        resp = self.client.post(reverse("exam-list"), {"reason": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_patient_id_and_inline_together_refuses(self):
        p = Patient.objects.create(doctor=self.user, name="Bia", age=28, sex="F")
        resp = self.client.post(reverse("exam-list"), {
            "patient_id": str(p.id), "name": "Bia", "age": 28, "sex": "F",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_inline_patient_requires_sex(self):
        resp = self.client.post(reverse("exam-list"), {"name": "Zé", "age": 50}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("sex", resp.data)

    def test_invalid_sex_refuses(self):
        for sex in ("X", "feminino", "f"):
            resp = self.client.post(reverse("exam-list"), {"name": "Zé", "age": 50, "sex": sex}, format="json")
            self.assertEqual(resp.status_code, 400, sex)
            self.assertIn("sex", resp.data)

    def test_age_range(self):
        for age, expected in ((-1, 400), (131, 400), (0, 201), (130, 201)):
            resp = self.client.post(reverse("exam-list"), {"name": "Zé", "age": age, "sex": "M"}, format="json")
            self.assertEqual(resp.status_code, expected, age)

    def test_create_exam_with_other_doctor_patient_refuses(self):
        p = Patient.objects.create(doctor=self.other, name="Lia", age=1, sex="F")
        resp = self.client.post(reverse("exam-list"), {"patient_id": str(p.id)}, format="json")
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

    def test_detail_of_other_doctor_is_404(self):
        exam = self._exame(doctor=self.other)
        resp = self.client.get(reverse("exam-detail", args=[exam.id]))
        self.assertEqual(resp.status_code, 404)


VALID_ATTEMPT = {"filename": "ecg.jpg", "content_type": "image/jpeg", "layout": "3x4+1"}


@mock.patch(PRESIGN_PUT, return_value="https://presigned/PUT")
class AttemptCreateTest(BaseAPITestCase):
    def _post(self, exam, **overrides):
        body = {**VALID_ATTEMPT, **overrides}
        body = {k: v for k, v in body.items() if v is not None}
        return self.client.post(reverse("attempt-create", args=[exam.id]), body, format="json")

    def test_creates_attempt_with_defaults(self, put):
        exam = self._exame()
        resp = self._post(exam)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["upload_url"], "https://presigned/PUT")
        self.assertEqual(resp.data["upload_headers"], {"Content-Type": "image/jpeg"})
        attempt = ExamAttempt.objects.get()
        self.assertEqual(str(attempt.id), resp.data["attempt_id"])
        self.assertTrue(attempt.image_s3_key.endswith(f"/{exam.id}/{attempt.id}/original.jpg"))
        self.assertEqual(attempt.status, ExamStatus.CREATED)
        self.assertEqual(attempt.layout, "3x4+1")
        self.assertEqual(attempt.calibration, {"speed_mm_s": 25, "gain_mm_mv": 10})
        self.assertIsNone(attempt.test_scenario)
        self.assertEqual(put.call_args.kwargs["content_type"], "image/jpeg")

    def test_only_reading_2_0_layouts_accepted(self, _put):
        exam = self._exame()
        for layout in ("3x4+1", "12x1"):
            self.assertEqual(self._post(exam, layout=layout).status_code, 201, layout)
        for layout in ("3x4", "3x4+3", "6x2", "6x2+1"):
            resp = self._post(exam, layout=layout)
            self.assertEqual(resp.status_code, 400, layout)
            self.assertIn("layout", resp.data)
        self.assertEqual(sorted(ExamAttempt.objects.values_list("layout", flat=True)), ["12x1", "3x4+1"])

    def test_layout_required_and_validated(self, _put):
        exam = self._exame()
        resp = self._post(exam, layout=None)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("layout", resp.data)
        resp = self._post(exam, layout="4x3")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("layout", resp.data)

    def test_calibration_values(self, _put):
        exam = self._exame()
        resp = self._post(exam, speed_mm_s=12.5, gain_mm_mv=20)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ExamAttempt.objects.get().calibration, {"speed_mm_s": 12.5, "gain_mm_mv": 20})
        self.assertEqual(self._post(exam, speed_mm_s=50.0, gain_mm_mv=5.0).status_code, 201)
        for field, value in (("speed_mm_s", 100), ("speed_mm_s", 5), ("gain_mm_mv", 1), ("gain_mm_mv", 2)):
            resp = self._post(exam, **{field: value})
            self.assertEqual(resp.status_code, 400, (field, value))
            self.assertIn(field, resp.data)

    def test_content_types(self, _put):
        exam = self._exame()
        for filename, content_type in (("a.png", "image/png"), ("a.jpeg", "image/jpeg"), ("a.WEBP", "image/webp")):
            resp = self._post(exam, filename=filename, content_type=content_type)
            self.assertEqual(resp.status_code, 201, filename)
        resp = self._post(exam, filename="a.gif", content_type="image/gif")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("content_type", resp.data)
        resp = self._post(exam, filename="a.heic", content_type="image/jpeg")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("filename", resp.data)

    def test_extension_must_match_content_type(self, _put):
        resp = self._post(self._exame(), filename="ecg.png", content_type="image/jpeg")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("content_type", resp.data)

    def test_new_attempt_resets_exam_status(self, _put):
        exam = self._exame()
        self._attempt(exam, ExamStatus.READING_REFUSED)
        exam.status = ExamStatus.READING_REFUSED
        exam.save()
        self.assertEqual(self._post(exam).status_code, 201)
        exam.refresh_from_db()
        self.assertEqual(exam.status, ExamStatus.CREATED)

    def test_foreign_exam_not_found(self, _put):
        exam = self._exame(doctor=self.other)
        resp = self._post(exam)
        self.assertEqual(resp.status_code, 404)


SCENARIO = {"read": {"scenario": "refused"},
            "interpret": {"scenario": "success_with_warnings"}}


@mock.patch(PRESIGN_PUT, return_value="https://presigned/PUT")
class TestScenarioGateTest(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user.is_staff = True
        self.user.save()
        self.exam = self._exame()

    def _post(self, scenario):
        return self.client.post(
            reverse("attempt-create", args=[self.exam.id]),
            {**VALID_ATTEMPT, "test_scenario": scenario}, format="json",
        )

    @override_settings(ECG_TEST_SCENARIOS_ENABLED=True)
    def test_staff_with_flag_is_accepted_and_stored(self, _put):
        resp = self._post(SCENARIO)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ExamAttempt.objects.get().test_scenario, SCENARIO)

    @override_settings(ECG_TEST_SCENARIOS_ENABLED=True)
    def test_non_staff_is_refused(self, _put):
        self.user.is_staff = False
        self.user.save()
        resp = self._post(SCENARIO)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("test_scenario", resp.data)
        self.assertFalse(ExamAttempt.objects.exists())

    @override_settings(ECG_TEST_SCENARIOS_ENABLED=False)
    def test_flag_off_is_refused_even_for_staff(self, _put):
        resp = self._post(SCENARIO)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("test_scenario", resp.data)

    @override_settings(ECG_TEST_SCENARIOS_ENABLED=False)
    def test_without_scenario_flag_is_irrelevant(self, _put):
        resp = self.client.post(reverse("attempt-create", args=[self.exam.id]), VALID_ATTEMPT, format="json")
        self.assertEqual(resp.status_code, 201)

    @override_settings(ECG_TEST_SCENARIOS_ENABLED=True)
    def test_scenario_catalog(self, _put):
        valid = [
            {"read": {"scenario": "success"}},
            {"read": {"scenario": "failure"}},
            {"read": {"scenario": "refused"}},
            {"read": {"scenario": "success_with_warnings"}},
            {"interpret": {"scenario": "failure", "code": "INTERP_NO_QRS_DETECTED"}},
        ]
        for scenario in valid:
            self.assertEqual(self._post(scenario).status_code, 201, scenario)
        invalid = [
            {},
            {"read": {"scenario": "explode"}},
            {"interpret": {"scenario": "refused"}},
            {"read": {"scenario": "refused", "code": "READ_REFUSED_BLURRY"}},
            {"read": {"scenario": "failure", "code": "STORAGE_ERROR"}},
            {"read": {"scenario": "success", "code": "READ_REFUSED_BLURRY"}},
            {"interpret": {"scenario": "failure", "code": "READ_GRID_NOT_FOUND"}},
            {"other": {"scenario": "success"}},
            "success",
        ]
        for scenario in invalid:
            resp = self._post(scenario)
            self.assertEqual(resp.status_code, 400, scenario)
            self.assertIn("test_scenario", resp.data)


class FeedbackTest(BaseAPITestCase):
    def test_feedback_created(self):
        attempt = self._attempt(self._exame())
        resp = self.client.post(reverse("attempt-feedback", args=[attempt.id]), {
            "issues": ["traçado errado"], "description": "QRS girado",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        fb = Feedback.objects.get()
        self.assertEqual(fb.user, self.user)
        self.assertEqual(fb.attempt, attempt)

    def test_feedback_foreign_attempt_404(self):
        attempt = self._attempt(self._exame(doctor=self.other))
        resp = self.client.post(reverse("attempt-feedback", args=[attempt.id]), {"issues": []}, format="json")
        self.assertEqual(resp.status_code, 404)
