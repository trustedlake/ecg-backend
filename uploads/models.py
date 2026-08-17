import uuid

from django.conf import settings
from django.db import models


class UploadBatch(models.Model):
    """Um envio de fotos pela página /upload. Função temporária — sem
    vínculo com Patient/Exam; só registra quem mandou o quê, confirmado
    ou não."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="upload_batches",
    )
    confirmado = models.BooleanField(default=False)
    confirmado_em = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Batch {self.id} ({self.user_id}, confirmado={self.confirmado})"

    class Meta:
        ordering = ["-created_at"]


class UploadedFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        UploadBatch, on_delete=models.CASCADE, related_name="files"
    )
    s3_key = models.CharField(max_length=500)
    content_type = models.CharField(max_length=100, null=True, blank=True)
    nome_original = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.nome_original or self.s3_key} (batch {self.batch_id})"
