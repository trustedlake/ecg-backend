"""Testes do comando ensure_admin (plano B4)."""

import io
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

User = get_user_model()


class EnsureAdminTests(TestCase):
    def run_cmd(self, **environ):
        out = io.StringIO()
        with mock.patch.dict(os.environ, environ):
            call_command("ensure_admin", stdout=out)
        return out.getvalue()

    def test_creates_superuser_once(self):
        output = self.run_cmd(ADMIN_USERNAME="admin", ADMIN_PASSWORD="Senha-forte-123")
        user = User.objects.get(username="admin")
        self.assertTrue(user.is_staff and user.is_superuser and user.is_active)
        self.assertTrue(user.check_password("Senha-forte-123"))
        self.assertNotIn("Senha-forte-123", output)

        output = self.run_cmd(ADMIN_USERNAME="admin", ADMIN_PASSWORD="outra-senha")
        self.assertEqual(User.objects.filter(username="admin").count(), 1)
        self.assertTrue(User.objects.get(username="admin").check_password("Senha-forte-123"))
        self.assertIn("já existe", output)

    def test_requires_env(self):
        with mock.patch.dict(os.environ, {"ADMIN_USERNAME": "", "ADMIN_PASSWORD": ""}):
            with self.assertRaises(CommandError):
                call_command("ensure_admin")
