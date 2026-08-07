import uuid

from django.conf import settings
from django.db import models


class ExamStatus(models.TextChoices):
    CRIADO = "criado", "Criado"
    FASE_A_PROCESSANDO = "fase_a_processando", "Fase A processando"
    AGUARDANDO_APROVACAO = "aguardando_aprovacao", "Aguardando aprovação"
    APROVADO = "aprovado", "Aprovado"
    REJEITADO = "rejeitado", "Rejeitado"
    FASE_B_PROCESSANDO = "fase_b_processando", "Fase B processando"
    CONCLUIDO = "concluido", "Concluído"
    ERRO = "erro", "Erro"


class FidelityVerdict(models.TextChoices):
    BOM = "bom", "Bom"
    PARCIAL = "parcial", "Parcial"
    RUIM = "ruim", "Ruim"


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="patients",
    )
    name = models.CharField(max_length=255)
    age = models.IntegerField()
    gender = models.CharField(max_length=50)
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
    # Único layout aceito no MVP (decisão #5): "3x4+1".
    ecg_layout = models.CharField(max_length=20, default="3x4+1")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Exam {self.id} — {self.patient.name}"


class ExamAttempt(models.Model):
    """Cada tentativa de processamento é uma linha imutável, nunca sobrescrita.
    Reprocessar = criar nova linha (histórico para treinar/melhorar o
    algoritmo)."""

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
    image_type = models.CharField(max_length=100, null=True, blank=True)
    calibracao = models.JSONField(null=True, blank=True)
    mascara_s3_key = models.CharField(max_length=255, null=True, blank=True)
    overlay_trace_s3_key = models.CharField(max_length=255, null=True, blank=True)
    overlay_signal_s3_key = models.CharField(max_length=255, null=True, blank=True)
    # sinais precisa sobreviver até a Fase B (lambda-3).
    sinais = models.JSONField(null=True, blank=True)
    lead_labels = models.JSONField(null=True, blank=True)
    rhythm_index = models.IntegerField(null=True, blank=True)
    # A lambda-2 retorna o veredito global da extração ("bom"/"parcial"/"ruim"),
    # não um float — ver lambda_2_signal_extraction/handler.py:117,152.
    fidelidade = models.CharField(
        max_length=20,
        choices=FidelityVerdict.choices,
        null=True,
        blank=True,
    )
    fidelidade_por_derivacao = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Attempt #{self.id} ({self.status})"

    class Meta:
        ordering = ["-created_at"]


class Report(models.Model):
    """Resultado da lambda-3, 1:1 com a tentativa que o gerou."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.OneToOneField(
        ExamAttempt, on_delete=models.CASCADE, related_name="report"
    )
    # A lambda-3 devolve `ritmo` como dict (ver handler.py:60-61) — portanto
    # JSONField, não string.
    ritmo = models.JSONField(null=True, blank=True)
    eixo_graus = models.FloatField(null=True, blank=True)
    sokolow_mm = models.FloatField(null=True, blank=True)
    derivacoes = models.JSONField(null=True, blank=True)
    indicadores = models.JSONField(null=True, blank=True)
    achados = models.JSONField(null=True, blank=True)
    conclusao = models.TextField(null=True, blank=True)
    # A lambda-3 devolve o veredito de fidelidade ("bom"/"parcial"/"ruim") como
    # `confiabilidade` (interpretation.py:206-209) — string, não float.
    confiabilidade = models.CharField(
        max_length=20,
        choices=FidelityVerdict.choices,
        null=True,
        blank=True,
    )
    versao_codigo = models.CharField(max_length=100, null=True, blank=True)
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