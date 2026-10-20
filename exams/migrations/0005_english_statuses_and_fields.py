from django.db import migrations, models

STATUS_CHOICES = [
    ("created", "Created"),
    ("reading", "Reading"),
    ("awaiting_validation", "Awaiting validation"),
    ("reading_refused", "Reading refused"),
    ("reading_error", "Reading error"),
    ("user_rejected", "Rejected by user"),
    ("interpreting", "Interpreting"),
    ("completed", "Completed"),
    ("interpretation_error", "Interpretation error"),
]

# Legacy stored status -> English status.
LEGACY_STATUS = {
    "criado": "created",
    "leitura_processando": "reading",
    "aguardando_validacao": "awaiting_validation",
    "leitura_recusada": "reading_refused",
    "erro_leitura": "reading_error",
    "rejeitado_usuario": "user_rejected",
    "interpretacao_processando": "interpreting",
    "concluido": "completed",
    "erro_interpretacao": "interpretation_error",
}


def _remap(apps, mapping):
    for model in ("Exam", "ExamAttempt"):
        Model = apps.get_model("exams", model)
        for old, new in mapping.items():
            Model.objects.filter(status=old).update(status=new)


def forwards(apps, schema_editor):
    _remap(apps, LEGACY_STATUS)


def backwards(apps, schema_editor):
    _remap(apps, {new: old for old, new in LEGACY_STATUS.items()})


class Migration(migrations.Migration):
    """English status values and field names."""

    dependencies = [
        ("exams", "0004_remove_legacy_pipeline_fields"),
    ]

    operations = [
        migrations.RenameField(model_name="examattempt", old_name="imagem_s3_key", new_name="image_s3_key"),
        migrations.RenameField(model_name="examattempt", old_name="calibracao", new_name="calibration"),
        migrations.AlterField(
            model_name="exam",
            name="status",
            field=models.CharField(choices=STATUS_CHOICES, default="created", max_length=40),
        ),
        migrations.AlterField(
            model_name="examattempt",
            name="status",
            field=models.CharField(choices=STATUS_CHOICES, default="created", max_length=40),
        ),
        migrations.AlterField(
            model_name="patient",
            name="sex",
            field=models.CharField(blank=True, choices=[("F", "Female"), ("M", "Male")], max_length=1, null=True),
        ),
        migrations.AlterField(
            model_name="report",
            name="classification",
            field=models.CharField(blank=True, choices=[("normal", "Normal"), ("borderline", "Borderline"), ("abnormal", "Abnormal")], max_length=20, null=True),
        ),
        migrations.RunPython(forwards, backwards),
    ]
