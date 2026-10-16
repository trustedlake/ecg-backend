"""Cria o superusuário inicial a partir de ADMIN_USERNAME/ADMIN_PASSWORD (plano B4).

Idempotente: se o usuário já existe, não altera nada (nem a senha). Nunca imprime a senha.
Roda no deploy (ci/deploy.sh) via run-task, depois do migrate.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Cria o superusuário ADMIN_USERNAME (senha em ADMIN_PASSWORD) se ele não existir."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not username or not password:
            raise CommandError("Defina ADMIN_USERNAME e ADMIN_PASSWORD.")

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin '{username}' já existe; nada a fazer.")
            return
        User.objects.create_superuser(
            username=username,
            password=password,
            email=os.environ.get("ADMIN_EMAIL", "").strip() or f"{username}@admin.local",
            name=os.environ.get("ADMIN_NAME", "").strip() or "Administrador",
        )
        self.stdout.write(f"Admin '{username}' criado.")
