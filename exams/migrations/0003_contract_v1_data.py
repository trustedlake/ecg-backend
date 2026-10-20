from django.db import migrations

# Estados do pipeline ecg-analyser → estados do contrato v1 (docs/user-flow-v1.md §3).
# Processamento interrompido vira erro: a tentativa não tem como continuar no fluxo novo.
STATUS_MAP = {
    "fase_a_processando": "erro_leitura",
    "aguardando_aprovacao": "aguardando_validacao",
    "aprovado": "concluido",
    "concluido": "concluido",
    "rejeitado": "rejeitado_usuario",
    "fase_b_processando": "erro_interpretacao",
    "erro": "erro_leitura",
}

SEX_MAP = {
    "f": "F", "feminino": "F", "female": "F", "mulher": "F",
    "m": "M", "masculino": "M", "male": "M", "homem": "M",
}

VALID_LAYOUTS = {"3x4", "3x4+1", "3x4+3", "6x2", "6x2+1", "12x1"}


def map_sex(gender):
    return SEX_MAP.get((gender or "").strip().lower())


def forwards(apps, schema_editor):
    Patient = apps.get_model("exams", "Patient")
    Exam = apps.get_model("exams", "Exam")
    ExamAttempt = apps.get_model("exams", "ExamAttempt")

    for patient in Patient.objects.all().only("id", "gender"):
        sex = map_sex(patient.gender)
        if sex:
            Patient.objects.filter(pk=patient.pk).update(sex=sex)

    for model in (Exam, ExamAttempt):
        for old, new in STATUS_MAP.items():
            model.objects.filter(status=old).update(status=new)

    # O layout passa do exame para a tentativa.
    for exam in Exam.objects.all().only("id", "ecg_layout"):
        if exam.ecg_layout in VALID_LAYOUTS:
            ExamAttempt.objects.filter(exam_id=exam.pk).update(layout=exam.ecg_layout)


class Migration(migrations.Migration):
    dependencies = [
        ("exams", "0002_contract_v1_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
