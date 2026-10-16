from django.db import migrations, models


class Migration(migrations.Migration):
    """Contrato v1: novos campos (os antigos só saem na 0004, depois da migração de dados)."""

    dependencies = [
        ("exams", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="sex",
            field=models.CharField(blank=True, choices=[("F", "Feminino"), ("M", "Masculino")], max_length=1, null=True),
        ),
        migrations.AddField(
            model_name="examattempt",
            name="layout",
            field=models.CharField(choices=[('3x4', '3x4'), ('3x4+1', '3x4+1'), ('3x4+3', '3x4+3'), ('6x2', '6x2'), ('6x2+1', '6x2+1'), ('12x1', '12x1')], default="3x4+1", max_length=10),
        ),
        migrations.AddField(
            model_name="examattempt",
            name="reading",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="examattempt",
            name="interpretation",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="examattempt",
            name="test_scenario",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="report",
            name="classification",
            field=models.CharField(blank=True, choices=[("normal", "Normal"), ("borderline", "Limítrofe"), ("abnormal", "Anormal")], max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="report",
            name="summary",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="report",
            name="details",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="report",
            name="disclaimer",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="report",
            name="warnings",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="report",
            name="engine",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="report",
            name="engine_version",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="exam",
            name="status",
            field=models.CharField(choices=[('criado', 'Criado'), ('leitura_processando', 'Leitura processando'), ('aguardando_validacao', 'Aguardando validação'), ('leitura_recusada', 'Leitura recusada'), ('erro_leitura', 'Erro na leitura'), ('rejeitado_usuario', 'Rejeitado pelo usuário'), ('interpretacao_processando', 'Interpretação processando'), ('concluido', 'Concluído'), ('erro_interpretacao', 'Erro na interpretação')], default="criado", max_length=40),
        ),
        migrations.AlterField(
            model_name="examattempt",
            name="status",
            field=models.CharField(choices=[('criado', 'Criado'), ('leitura_processando', 'Leitura processando'), ('aguardando_validacao', 'Aguardando validação'), ('leitura_recusada', 'Leitura recusada'), ('erro_leitura', 'Erro na leitura'), ('rejeitado_usuario', 'Rejeitado pelo usuário'), ('interpretacao_processando', 'Interpretação processando'), ('concluido', 'Concluído'), ('erro_interpretacao', 'Erro na interpretação')], default="criado", max_length=40),
        ),
    ]
