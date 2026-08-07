from django.contrib import admin

from .models import Exam, ExamAttempt, Feedback, Patient, Report


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "age", "gender", "doctor", "created_at"]
    list_filter = ["gender", "created_at"]
    search_fields = ["name", "doctor__email"]


class AttemptInline(admin.TabularInline):
    model = ExamAttempt
    fields = [
        "status", "imagem_s3_key", "image_type", "rhythm_index",
        "fidelidade", "created_at",
    ]
    readonly_fields = ["created_at"]
    extra = 0


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ["id", "patient", "doctor", "status", "ecg_layout", "created_at"]
    list_filter = ["status", "ecg_layout", "created_at"]
    search_fields = ["patient__name", "doctor__email", "reason"]
    inlines = [AttemptInline]


@admin.register(ExamAttempt)
class ExamAttemptAdmin(admin.ModelAdmin):
    list_display = [
        "id", "exam", "status", "image_type", "rhythm_index",
        "fidelidade", "created_at",
    ]
    list_filter = ["status", "image_type", "created_at"]
    search_fields = ["exam__patient__name", "imagem_s3_key"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "confiabilidade", "eixo_graus", "created_at"]
    list_filter = ["confiabilidade", "created_at"]
    search_fields = ["attempt__exam__patient__name", "versao_codigo"]


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "user", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["attempt__exam__patient__name", "user__email"]