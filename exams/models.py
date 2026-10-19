import uuid

from django.conf import settings
from django.db import models


class ExamStatus(models.TextChoices):
    """Estados da tentativa (docs/fluxo-usuario-v1.md §3). O Exam espelha a mais recente."""

    CRIADO = "criado", "Criado"
    LEITURA_PROCESSANDO = "leitura_processando", "Leitura processando"
    AGUARDANDO_VALIDACAO = "aguardando_validacao", "Aguardando validação"
    LEITURA_RECUSADA = "leitura_recusada", "Leitura recusada"
    ERRO_LEITURA = "erro_leitura", "Erro na leitura"
    REJEITADO_USUARIO = "rejeitado_usuario", "Rejeitado pelo usuário"
    INTERPRETACAO_PROCESSANDO = "interpretacao_processando", "Interpretação processando"
    CONCLUIDO = "concluido", "Concluído"
    ERRO_INTERPRETACAO = "erro_interpretacao", "Erro na interpretação"


class Sex(models.TextChoices):
    F = "F", "Feminino"
    M = "M", "Masculino"


class EcgLayout(models.TextChoices):
    """Layouts do contrato antigo. A leitura 2.0 só aceita SUPPORTED_LAYOUTS; os demais ficam
    aqui para os registros já gravados."""

    L3X4 = "3x4", "3x4"
    L3X4_1 = "3x4+1", "3x4+1"
    L3X4_3 = "3x4+3", "3x4+3"
    L6X2 = "6x2", "6x2"
    L6X2_1 = "6x2+1", "6x2+1"
    L12X1 = "12x1", "12x1"


# Layouts com configuração na ecg-reading 2.0 (CONTRATO_LAMBDAS.md §2); os demais dão LAYOUT_UNSUPPORTED.
SUPPORTED_LAYOUTS = (EcgLayout.L3X4_1, EcgLayout.L12X1)


class Classification(models.TextChoices):
    NORMAL = "normal", "Normal"
    BORDERLINE = "borderline", "Limítrofe"
    ABNORMAL = "abnormal", "Anormal"


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="patients",
    )
    name = models.CharField(max_length=255)
    age = models.IntegerField()
    # Usado na interpretação (CONTRATO_LAMBDAS.md §3.1). Nulo só em pacientes legados.
    sex = models.CharField(max_length=1, choices=Sex.choices, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.age} anos)"


class Exam(models.Model):
    """Encontro clínico: paciente + médico + motivo. Leve — nunca guarda
    dado de processamento diretamente (isso vive em ExamAttempt)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="exams"
    )
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="exams",
    )
    status = models.CharField(
        max_length=40, choices=ExamStatus.choices, default=ExamStatus.CRIADO
    )
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Exam {self.id} — {self.patient.name}"


class ExamAttempt(models.Model):
    """Uma foto do ECG passando pela leitura e pela interpretação.

    Recusar ou refazer com outra foto = criar nova linha (histórico preservado).

    `reading` (gravado pela leitura, sem `samples` — o sinal fica no S3)::

        {status, warnings, error, meta, grid,
         leads: [{name, kind, quality, coverage}],
         artifacts: {overlay: {bucket, key}, signal: {bucket, key},
                     refusal_preview: {bucket, key} | null},
         refusal_region_px}

    `interpretation`: ``{status, warnings, error, meta}``; o resultado clínico vai para `Report`.

    `test_scenario`: ``{"read": {"scenario", "code"?}, "interpret": {...}}`` — repassado como
    `mock` às lambdas (CONTRATO_LAMBDAS.md §4); só staff com ECG_TEST_SCENARIOS_ENABLED.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam = models.ForeignKey(
        Exam, on_delete=models.CASCADE, related_name="attempts"
    )
    status = models.CharField(
        max_length=40,
        choices=ExamStatus.choices,
        default=ExamStatus.CRIADO,
    )
    imagem_s3_key = models.CharField(max_length=500)
    layout = models.CharField(max_length=10, choices=EcgLayout.choices, default=EcgLayout.L3X4_1)
    calibracao = models.JSONField(null=True, blank=True)
    reading = models.JSONField(null=True, blank=True)
    interpretation = models.JSONField(null=True, blank=True)
    test_scenario = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Attempt #{self.id} ({self.status})"

    class Meta:
        ordering = ["-created_at"]


class Report(models.Model):
    """Resultado da interpretação (CONTRATO_LAMBDAS.md §3.2), 1:1 com a tentativa."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.OneToOneField(
        ExamAttempt, on_delete=models.CASCADE, related_name="report"
    )
    classification = models.CharField(
        max_length=20, choices=Classification.choices, null=True, blank=True
    )
    summary = models.JSONField(null=True, blank=True)
    details = models.JSONField(null=True, blank=True)
    disclaimer = models.TextField(blank=True, default="")
    warnings = models.JSONField(default=list, blank=True)
    engine = models.CharField(max_length=100, blank=True, default="")
    engine_version = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Report ({self.attempt_id})"


class Feedback(models.Model):
    """Crítica do médico sobre uma tentativa específica."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.ForeignKey(
        ExamAttempt, on_delete=models.CASCADE, related_name="feedback_items"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="feedback",
    )
    issues = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Feedback #{self.id} — attempt {self.attempt_id}"