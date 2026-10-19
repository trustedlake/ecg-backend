from django.db import migrations


class Migration(migrations.Migration):
    """Remove os campos do pipeline ecg-analyser (refine e fidelidade saíram da V1)."""

    dependencies = [
        ("exams", "0003_contract_v1_data"),
    ]

    operations = [
        migrations.RemoveField(model_name="patient", name="gender"),
        migrations.RemoveField(model_name="exam", name="ecg_layout"),
        migrations.RemoveField(model_name="examattempt", name="image_type"),
        migrations.RemoveField(model_name="examattempt", name="mascara_s3_key"),
        migrations.RemoveField(model_name="examattempt", name="overlay_trace_s3_key"),
        migrations.RemoveField(model_name="examattempt", name="overlay_signal_s3_key"),
        migrations.RemoveField(model_name="examattempt", name="sinais"),
        migrations.RemoveField(model_name="examattempt", name="lead_labels"),
        migrations.RemoveField(model_name="examattempt", name="rhythm_index"),
        migrations.RemoveField(model_name="examattempt", name="fidelidade"),
        migrations.RemoveField(model_name="examattempt", name="fidelidade_por_derivacao"),
        migrations.RemoveField(model_name="report", name="ritmo"),
        migrations.RemoveField(model_name="report", name="eixo_graus"),
        migrations.RemoveField(model_name="report", name="sokolow_mm"),
        migrations.RemoveField(model_name="report", name="derivacoes"),
        migrations.RemoveField(model_name="report", name="indicadores"),
        migrations.RemoveField(model_name="report", name="achados"),
        migrations.RemoveField(model_name="report", name="conclusao"),
        migrations.RemoveField(model_name="report", name="confiabilidade"),
        migrations.RemoveField(model_name="report", name="versao_codigo"),
    ]
