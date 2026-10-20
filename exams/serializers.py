from rest_framework import serializers

from django.conf import settings

from . import services
from .models import SUPPORTED_LAYOUTS, Exam, ExamAttempt, Feedback, Patient, Sex

CONTENT_TYPE_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
EXT_BY_CONTENT_TYPE = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

SPEED_CHOICES = [12.5, 25, 50]
GAIN_CHOICES = [5, 10, 20]

# Scenarios for the lambdas' `mock` field (ecg-lambdas-v1): scenario -> accepted codes.
# ecg-reading 2.0 only receives the scenario; it picks the refusal/error code itself.
READ_SCENARIOS = {
    "success": (),
    "success_with_warnings": (),
    "failure": (),
    "refused": (),
}
INTERPRET_SCENARIOS = {
    "success": (),
    "success_with_warnings": (),
    "failure": ("INTERP_NO_QRS_DETECTED", "INTERP_SIGNAL_TOO_NOISY", "INTERNAL_ERROR"),
}


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["id", "name", "age", "sex"]
        read_only_fields = ["id"]


class ExamCreateSerializer(serializers.Serializer):
    """Creates an exam with patient_id (the doctor's existing patient) OR an inline patient."""

    patient_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, required=False)
    age = serializers.IntegerField(required=False, min_value=0, max_value=130)
    sex = serializers.ChoiceField(choices=Sex.choices, required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        has_patient_id = bool(attrs.get("patient_id"))
        has_inline = any(attrs.get(f) is not None for f in ("name", "age", "sex"))
        if has_patient_id == has_inline:
            raise serializers.ValidationError(
                "Provide patient_id (existing patient) OR the patient "
                "data (name, age, sex): exactly one of the two."
            )
        if has_inline:
            missing = [f for f in ("name", "age", "sex") if attrs.get(f) in (None, "")]
            if missing:
                raise serializers.ValidationError(
                    {f: "Required for a new patient." for f in missing}
                )
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        if validated_data.get("patient_id"):
            try:
                patient = Patient.objects.get(
                    id=validated_data["patient_id"], doctor=user
                )
            except Patient.DoesNotExist:
                raise serializers.ValidationError(
                    {"patient_id": "Patient not found for this doctor."}
                )
        else:
            patient = Patient.objects.create(
                doctor=user,
                name=validated_data["name"],
                age=validated_data["age"],
                sex=validated_data["sex"],
            )
        return Exam.objects.create(
            patient=patient,
            doctor=user,
            reason=validated_data.get("reason", ""),
        )


class AttemptSerializer(serializers.ModelSerializer):
    """API `Attempt` object (plan §0.1). Presigned URLs generated on every response."""

    exam_id = serializers.UUIDField(read_only=True)
    calibration = serializers.JSONField()
    original_image_url = serializers.SerializerMethodField()
    reading = serializers.SerializerMethodField()
    interpretation = serializers.SerializerMethodField()
    can_retry = serializers.SerializerMethodField()
    needs_new_photo = serializers.SerializerMethodField()

    class Meta:
        model = ExamAttempt
        fields = [
            "id", "exam_id", "status", "layout", "calibration", "original_image_url",
            "reading", "interpretation", "can_retry", "needs_new_photo",
            "created_at", "updated_at",
        ]

    def get_original_image_url(self, obj):
        return services.original_image_url(obj, settings.PRESIGN_URL_EXPIRES)

    def get_reading(self, obj):
        return services.attempt_reading_view(obj)

    def get_interpretation(self, obj):
        return services.attempt_interpretation_view(obj)

    def get_can_retry(self, obj):
        return services.attempt_flags(obj)["can_retry"]

    def get_needs_new_photo(self, obj):
        return services.attempt_flags(obj)["needs_new_photo"]


class ExamSerializer(serializers.ModelSerializer):
    patient = PatientSerializer(read_only=True)

    class Meta:
        model = Exam
        fields = ["id", "patient", "status", "reason", "created_at", "updated_at"]


class ExamDetailSerializer(ExamSerializer):
    attempts = serializers.SerializerMethodField()

    class Meta(ExamSerializer.Meta):
        fields = ExamSerializer.Meta.fields + ["attempts"]

    def get_attempts(self, obj):
        attempts = obj.attempts.all()
        return AttemptSerializer(attempts, many=True, context=self.context).data


class ScenarioSerializer(serializers.Serializer):
    scenario = serializers.CharField()
    code = serializers.CharField(required=False)

    def __init__(self, *args, catalog, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog = catalog

    def validate(self, attrs):
        scenario = attrs["scenario"]
        if scenario not in self.catalog:
            raise serializers.ValidationError(
                {"scenario": f"Invalid scenario. Accepted: {', '.join(self.catalog)}."}
            )
        code = attrs.get("code")
        if code is not None and code not in self.catalog[scenario]:
            allowed = ", ".join(self.catalog[scenario]) or "none"
            raise serializers.ValidationError(
                {"code": f"Invalid code for '{scenario}'. Accepted: {allowed}."}
            )
        return attrs


class TestScenarioSerializer(serializers.Serializer):
    """`{"read": {...}, "interpret": {...}}`, forwarded as `mock` to the lambdas."""

    read = ScenarioSerializer(required=False, catalog=READ_SCENARIOS)
    interpret = ScenarioSerializer(required=False, catalog=INTERPRET_SCENARIOS)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = set(data) - {"read", "interpret"}
            if unknown:
                raise serializers.ValidationError(
                    f"Unsupported keys: {', '.join(sorted(unknown))}."
                )
        return super().to_internal_value(data)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide 'read' and/or 'interpret'.")
        return attrs


class AttemptCreateSerializer(serializers.Serializer):
    """Creates the attempt and returns the presigned PUT URL for the app to upload the photo.

    The `content_type` is signed into the URL: the app MUST send the same value in the
    PUT Content-Type header (returned in `upload_headers`).
    """

    filename = serializers.CharField(max_length=255)
    content_type = serializers.ChoiceField(choices=list(EXT_BY_CONTENT_TYPE))
    layout = serializers.ChoiceField(choices=[(layout.value, layout.label) for layout in SUPPORTED_LAYOUTS])
    # FloatField + explicit check: ChoiceField compares as text and would reject 25.0.
    speed_mm_s = serializers.FloatField(default=25)
    gain_mm_mv = serializers.FloatField(default=10)
    test_scenario = TestScenarioSerializer(required=False)

    def validate_filename(self, value):
        dot = value.rfind(".")
        ext = value[dot + 1:].lower() if dot != -1 else ""
        if ext not in CONTENT_TYPE_BY_EXT:
            raise serializers.ValidationError(
                "Unsupported extension; send JPG, PNG or WebP."
            )
        return value

    def validate_speed_mm_s(self, value):
        if value not in SPEED_CHOICES:
            raise serializers.ValidationError("Accepted speed: 12.5, 25 or 50 mm/s.")
        return value

    def validate_gain_mm_mv(self, value):
        if value not in GAIN_CHOICES:
            raise serializers.ValidationError("Accepted gain: 5, 10 or 20 mm/mV.")
        return value

    def validate_test_scenario(self, value):
        user = self.context["request"].user
        if not (settings.ECG_TEST_SCENARIOS_ENABLED and user.is_staff):
            raise serializers.ValidationError(
                "Test scenarios are not available for this user."
            )
        return value

    def validate(self, attrs):
        ext = attrs["filename"].rsplit(".", 1)[1].lower()
        if CONTENT_TYPE_BY_EXT[ext] != attrs["content_type"]:
            raise serializers.ValidationError(
                {"content_type": "The type does not match the file extension."}
            )
        attrs["_ext"] = EXT_BY_CONTENT_TYPE[attrs["content_type"]]
        return attrs


class FeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Feedback
        fields = ["id", "attempt_id", "user_id", "issues", "description", "created_at"]
        read_only_fields = ["id", "attempt_id", "user_id", "created_at"]

    def create(self, validated_data):
        validated_data["attempt"] = self.context["attempt"]
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)
