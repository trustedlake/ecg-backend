import uuid

from django.conf import settings
from django.db import models


class UploadBatch(models.Model):
    """A photo submission from the /upload page. Temporary feature: no
    link to Patient/Exam; it only records who sent what, confirmed
    or not."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="upload_batches",
    )
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Batch {self.id} ({self.user_id}, confirmed={self.confirmed})"

    class Meta:
        ordering = ["-created_at"]


class UploadedFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        UploadBatch, on_delete=models.CASCADE, related_name="files"
    )
    s3_key = models.CharField(max_length=500)
    content_type = models.CharField(max_length=100, null=True, blank=True)
    original_name = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.original_name or self.s3_key} (batch {self.batch_id})"
