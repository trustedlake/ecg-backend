from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """UserAdmin adaptado: email é o identificador (sem username)."""

    ordering = ["email"]
    list_display = ["email", "name", "crm", "is_staff", "is_active"]
    search_fields = ["email", "name", "crm"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Dados", {"fields": ("name", "crm")}),
        ("Status", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("Datas", {"fields": ("last_login", "created_at", "updated_at")}),
        ("Permissões", {"fields": ("groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "name", "password1", "password2")}),
    )
    readonly_fields = ["created_at", "updated_at", "last_login"]