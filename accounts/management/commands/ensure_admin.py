"""Creates the initial superuser from ADMIN_USERNAME/ADMIN_PASSWORD (plan B4).

Idempotent: if the user already exists, nothing changes (not even the password). Never prints the password.
Runs during deploy (ci/deploy.sh) via run-task, after migrate.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Creates the ADMIN_USERNAME superuser (password in ADMIN_PASSWORD) if it does not exist."

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not username or not password:
            raise CommandError("Set ADMIN_USERNAME and ADMIN_PASSWORD.")

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin '{username}' already exists; nothing to do.")
            return
        User.objects.create_superuser(
            username=username,
            password=password,
            email=os.environ.get("ADMIN_EMAIL", "").strip() or f"{username}@admin.local",
            name=os.environ.get("ADMIN_NAME", "").strip() or "Administrator",
        )
        self.stdout.write(f"Admin '{username}' created.")
