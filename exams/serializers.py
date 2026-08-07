from rest_framework import serializers

from django.conf import settings

from . import services
from .models import Exam, ExamAttempt, Feedback, Patient, Report

VALID_IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "webp", "tiff"}


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["id", "name", "age", "gender"]
        read_only_fields = ["id"]


class ExamCreateSerializer(serializers.Serializer):
    """Cria exame com patient_id (existente do médico) OU paciente inline."""

    patient_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, required=False)
    age = serializers.IntegerField(required=False)
    gender = serializers.CharField(max_length=50, required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    ecg_layout = serializers.CharField(max_length=20, default="3x4+1")

    def validate(self, attrs):
        has_patient_id = bool(attrs.get("patient_id"))
        has_inline = bool(attrs.get("name"))
        if has_patient_id == has_inline:
            raise serializers.ValidationError(
                "Informe patient_id (paciente existente) OU os dados do "
                "paciente (name, age, gender) — exatamente um dos dois."
            )
        if has_inline and not (attrs.get("age") is not None and attrs.get("gender")):
            raise serializers.ValidationError(
                "Paciente novo exige age e gender junto com name."
            )
        layout = attrs.get("ecg_layout")
        if layout != "3x4+1":
            raise serializers.ValidationError(
                {"ecg_layout": "Único layout aceito no MVP: '3x4+1'."}
            )
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        patient = None
        if validated_data.get("patient_id"):
            try:
                patient = Patient.objects.get(
                    id=validated_data["patient_id"], doctor=user
                )
            except Patient.DoesNotExist:
                raise serializers.ValidationError(
                    {"patient_id": "Paciente não encontrado para este médico."}
                )
        else:
            patient = Patient.objects.create(
                doctor=user,
                name=validated_data["name"],
                age=validated_data["age"],
                gender=validated_data["gender"],
            )
        return Exam.objects.create(
            patient=patient,
            doctor=user,
            reason=validated_data.get("reason", ""),
            ecg_layout=validated_data.get("ecg_layout", "3x4+1"),
        )


class ReportSerializer(serializers.ModelSerializer):
    attempt_id = serializers.UUIDField()

    class Meta:
        model = Report
        fields = [
            "id", "attempt_id", "ritmo", "eixo_graus", "sokolow_mm",
            "derivacoes", "indicadores", "achados", "conclusao",
            "confiabilidade", "versao_codigo", "created_at",
        ]


class ExamAttemptSerializer(serializers.ModelSerializer):
    """Tentativa no detalhe do exame: dados salvos + URLs presigned on-demand."""

    original_url = serializers.SerializerMethodField()
    overlay_trace_url = serializers.SerializerMethodField()
    overlay_signal_url = serializers.SerializerMethodField()
    report = serializers.SerializerMethodField()

    class Meta:
        model = ExamAttempt
        fields = [
            "id", "exam_id", "status", "imagem_s3_key", "image_type",
            "calibracao", "mascara_s3_key", "overlay_trace_s3_key",
            "overlay_signal_s3_key", "rhythm_index", "fidelidade",
            "fidelidade_por_derivacao", "created_at", "updated_at",
            "original_url", "overlay_trace_url", "overlay_signal_url",
            "report",
        ]

    def _attempt(self, obj):
        return obj

    def get_original_url(self, obj):
        return services.original_image_url(obj, settings.PRESIGN_URL_EXPIRES)

    def get_overlay_trace_url(self, obj):
        return services.overlay_urls(obj, settings.PRESIGN_URL_EXPIRES)["overlay_trace_url"]

    def get_overlay_signal_url(self, obj):
        return services.overlay_urls(obj, settings.PRESIGN_URL_EXPIRES)["overlay_signal_url"]

    def get_report(self, obj):
        report = getattr(obj, "report", None)
        if report is None:
            return None
        return ReportSerializer(report).data


class ExamSerializer(serializers.ModelSerializer):
    patient = PatientSerializer(read_only=True)

    class Meta:
        model = Exam
        fields = [
            "id", "patient", "status", "reason", "ecg_layout",
            "created_at", "updated_at",
        ]


class ExamDetailSerializer(ExamSerializer):
    attempts = serializers.SerializerMethodField()

    class Meta(ExamSerializer.Meta):
        fields = ExamSerializer.Meta.fields + ["attempts"]

    def get_attempts(self, obj):
        attempts = obj.attempts.all()
        return ExamAttemptSerializer(attempts, many=True, context=self.context).data


class AttemptCreateSerializer(serializers.Serializer):
    """Cria a attempt e devolve presigned PUT URL pro frontend subir a imagem.

    filename (opcional): deriva a extensão do key ('original.<ext>').
    content_type (opcional): se informado, é assinado no presigned PUT — o
    frontend DEVE mandar esse mesmo valor no header Content-Type.
    """

    filename = serializers.CharField(required=False, allow_blank=True)
    content_type = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        ext = "png"
        filename = attrs.get("filename", "")
        content_type = attrs.get("content_type", "")
        if filename:
            dot = filename.rfind(".")
            if dot != -1:
                ext = filename[dot + 1:].lower()
            else:
                ext = filename.lower()
        elif content_type == "image/jpeg":
            ext = "jpg"
        elif content_type.startswith("image/"):
            ext = content_type.split("/")[1] or "png"
        if ext not in VALID_IMAGE_EXTS:
            raise serializers.ValidationError(
                {"filename": f"Extensão de imagem não suportada: '{ext}'."}
            )
        attrs["_ext"] = ext
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