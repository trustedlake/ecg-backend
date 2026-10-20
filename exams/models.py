import uuid

from django.conf import settings
from django.db import models


class ExamStatus(models.TextChoices):
    """Attempt states (docs/user-flow-v1.md §3). The Exam mirrors the latest one."""

    CREATED = "created", "Created"
    READING = "reading", "Reading"
    AWAITING_VALIDATION = "awaiting_validation", "Awaiting validation"
    READING_REFUSED = "reading_refused", "Reading refused"
    READING_ERROR = "reading_error", "Reading error"
    USER_REJECTED = "user_rejected", "Rejected by user"
    INTERPRETING = "interpreting", "Interpreting"
    COMPLETED = "completed", "Completed"
    INTERPRETATION_ERROR = "interpretation_error", "Interpretation error"


class Sex(models.TextChoices):
    F = "F", "Female"
    M = "M", "Male"


class EcgLayout(models.TextChoices):
    """Old-contract layouts. Reading 2.0 only accepts SUPPORTED_LAYOUTS; the others stay
    here for records already stored."""

    L3X4 = "3x4", "3x4"
    L3X4_1 = "3x4+1", "3x4+1"
    L3X4_3 = "3x4+3", "3x4+3"
    L6X2 = "6x2", "6x2"
    L6X2_1 = "6x2+1", "6x2+1"
    L12X1 = "12x1", "12x1"


# Layouts configured in ecg-reading 2.0 (the lambda contract, §2); the others return LAYOUT_UNSUPPORTED.
SUPPORTED_LAYOUTS = (EcgLayout.L3X4_1, EcgLayout.L12X1)


class Classification(models.TextChoices):
    NORMAL = "normal", "Normal"
    BORDERLINE = "borderline", "Borderline"
    ABNORMAL = "abnormal", "Abnormal"


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="patients",
    )
    name = models.CharField(max_length=255)
    age = models.IntegerField()
    # Used by the interpretation (lambda contract §3.1). Null only for legacy patients.
    sex = models.CharField(max_length=1, choices=Sex.choices, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.age} years)"


class Exam(models.Model):
    """Clinical encounter: patient + doctor + reason. Lightweight: never stores
    processing data directly (that lives in ExamAttempt)."""

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
        max_length=40, choices=ExamStatus.choices, default=ExamStatus.CREATED
    )
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Exam {self.id} — {self.patient.name}"


class ExamAttempt(models.Model):
    """One ECG photo going through reading and interpretation.

    Rejecting or redoing with another photo = a new row (history preserved).

    `reading` (written by the reading, without `samples`: the signal stays in S3)::

        {status, warnings, error, meta, grid,
         leads: [{name, kind, quality, coverage}],
         artifacts: {overlay: {bucket, key}, signal: {bucket, key},
                     refusal_preview: {bucket, key} | null},
         refusal_region_px}

    `interpretation`: ``{status, warnings, error, meta}``; the clinical result goes to `Report`.

    `test_scenario`: ``{"read": {"scenario", "code"?}, "interpret": {...}}``, forwarded as
    `mock` to the lambdas (lambda contract §4); staff only, with ECG_TEST_SCENARIOS_ENABLED.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam = models.ForeignKey(
        Exam, on_delete=models.CASCADE, related_name="attempts"
    )
    status = models.CharField(
        max_length=40,
        choices=ExamStatus.choices,
        default=ExamStatus.CREATED,
    )
    image_s3_key = models.CharField(max_length=500)
    layout = models.CharField(max_length=10, choices=EcgLayout.choices, default=EcgLayout.L3X4_1)
    calibration = models.JSONField(null=True, blank=True)
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
    """Interpretation result (lambda contract §3.2), 1:1 with the attempt."""

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
    """Doctor feedback on a specific attempt."""

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