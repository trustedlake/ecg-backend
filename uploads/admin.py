from django.contrib import admin

from .models import UploadBatch, UploadedFile


class UploadedFileInline(admin.TabularInline):
    model = UploadedFile
    fields = ["s3_key", "content_type", "nome_original", "created_at"]
    readonly_fields = ["created_at"]
    extra = 0


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "confirmado", "confirmado_em", "created_at"]
    list_filter = ["confirmado", "created_at"]
    search_fields = ["user__email"]
    inlines = [UploadedFileInline]
