from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase

User = get_user_model()


class LoginTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="medica", password="senha-123", email="m@x.com", name="M")

    def login(self, username, password, ip="203.0.113.7"):
        return self.client.post(reverse("login"), {"username": username, "password": password},
                                format="json", HTTP_X_FORWARDED_FOR=ip)

    def test_same_message_for_unknown_user_wrong_password_and_inactive(self):
        wrong = self.login("medica", "errada")
        unknown = self.login("ninguem", "senha-123")
        self.user.is_active = False
        self.user.save()
        inactive = self.login("medica", "senha-123")
        for response in (wrong, unknown, inactive):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"detail": "Usuário ou senha inválidos."})

    def test_success_is_case_insensitive(self):
        response = self.login("MEDICA", "senha-123")
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

    def test_throttled_per_ip(self):
        for _ in range(10):
            self.assertEqual(self.login("medica", "errada").status_code, 400)
        self.assertEqual(self.login("medica", "senha-123").status_code, 429)
        self.assertEqual(self.login("medica", "senha-123", ip="198.51.100.9").status_code, 200)
