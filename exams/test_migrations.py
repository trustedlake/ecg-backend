"""Data migration from the legacy ecg-analyser pipeline to contract v1 (exams 0002-0004)."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("exams", "0001_initial")]
AFTER = [("exams", "0004_remove_legacy_pipeline_fields")]


class ContractV1DataMigrationTest(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        old = executor.loader.project_state(BEFORE + [("accounts", "0002_user_username")]).apps
        User = old.get_model("accounts", "User")
        Patient = old.get_model("exams", "Patient")
        Exam = old.get_model("exams", "Exam")
        Attempt = old.get_model("exams", "ExamAttempt")

        doctor = User.objects.create(username="doc", email="doc@test.local")
        self.patients = {}
        for gender in ("F", "f", "Feminino", "female", "Mulher", "M", "masculino", "Male", "HOMEM", "outro", ""):
            self.patients[gender] = Patient.objects.create(doctor=doctor, name=gender or "vazio", age=40, gender=gender)

        self.exams = {}
        patient = self.patients["F"]
        for old_status in ("criado", "fase_a_processando", "aguardando_aprovacao", "aprovado",
                           "rejeitado", "fase_b_processando", "concluido", "erro"):
            exam = Exam.objects.create(patient=patient, doctor=doctor, status=old_status, ecg_layout="6x2")
            Attempt.objects.create(exam=exam, status=old_status, imagem_s3_key="k", fidelidade="bom")
            self.exams[old_status] = exam.pk

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(AFTER)
        self.apps = executor.loader.project_state(AFTER).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_sex_mapping(self):
        Patient = self.apps.get_model("exams", "Patient")
        expected = {"F": "F", "f": "F", "Feminino": "F", "female": "F", "Mulher": "F",
                    "M": "M", "masculino": "M", "Male": "M", "HOMEM": "M", "outro": None, "": None}
        for gender, sex in expected.items():
            self.assertEqual(Patient.objects.get(pk=self.patients[gender].pk).sex, sex, gender)

    def test_status_mapping_on_exam_and_attempt(self):
        Exam = self.apps.get_model("exams", "Exam")
        Attempt = self.apps.get_model("exams", "ExamAttempt")
        expected = {
            "criado": "criado",
            "fase_a_processando": "erro_leitura",
            "aguardando_aprovacao": "aguardando_validacao",
            "aprovado": "concluido",
            "rejeitado": "rejeitado_usuario",
            "fase_b_processando": "erro_interpretacao",
            "concluido": "concluido",
            "erro": "erro_leitura",
        }
        for old_status, new_status in expected.items():
            exam_id = self.exams[old_status]
            self.assertEqual(Exam.objects.get(pk=exam_id).status, new_status, old_status)
            self.assertEqual(Attempt.objects.get(exam_id=exam_id).status, new_status, old_status)

    def test_layout_moves_from_exam_to_attempt(self):
        Attempt = self.apps.get_model("exams", "ExamAttempt")
        self.assertEqual(set(Attempt.objects.values_list("layout", flat=True)), {"6x2"})
