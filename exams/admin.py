from django.contrib import admin

from .models import Exam, ExamAttempt, Feedback, Patient, Report


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "age", "sex", "doctor", "created_at"]
    list_filter = ["sex", "created_at"]
    search_fields = ["name", "doctor__email"]


class AttemptInline(admin.TabularInline):
    model = ExamAttempt
    fields = ["status", "layout", "image_s3_key", "created_at"]
    readonly_fields = ["created_at"]
    extra = 0


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ["id", "patient", "doctor", "status", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["patient__name", "doctor__email", "reason"]
    inlines = [AttemptInline]


@admin.register(ExamAttempt)
class ExamAttemptAdmin(admin.ModelAdmin):
    list_display = ["id", "exam", "status", "layout", "created_at"]
    list_filter = ["status", "layout", "created_at"]
    search_fields = ["exam__patient__name", "image_s3_key"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "classification", "engine", "created_at"]
    list_filter = ["classification", "created_at"]
    search_fields = ["attempt__exam__patient__name", "engine_version"]


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ["id", "attempt", "user", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["attempt__exam__patient__name", "user__email"]
