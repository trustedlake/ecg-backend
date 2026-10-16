"""Testes de produção (plano B4): SPA catch-all, 404 JSON da API, banco por DB_*."""

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from config import settings as project_settings


class SpaTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dist = Path(tmp.name)

    def test_routes_serve_index_html(self):
        (self.dist / "index.html").write_text("<html>app ecg</html>")
        with override_settings(FRONTEND_DIST=self.dist):
            for url in ["/", "/exames/123", "/login", "/apiary", "/administracao"]:
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(b"".join(response.streaming_content), b"<html>app ecg</html>")
                    self.assertEqual(response["Cache-Control"], "no-cache")

    def test_missing_dist_is_clear_404(self):
        with override_settings(FRONTEND_DIST=self.dist):
            response = self.client.get("/exames")
        self.assertEqual(response.status_code, 404)
        self.assertIn("frontend_dist/index.html", response.content.decode())

    def test_api_404_stays_json(self):
        (self.dist / "index.html").write_text("<html></html>")
        with override_settings(FRONTEND_DIST=self.dist):
            response = self.client.get("/api/inexistente/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Not found."})

    def test_admin_and_static_not_swallowed(self):
        (self.dist / "index.html").write_text("<html></html>")
        with override_settings(FRONTEND_DIST=self.dist):
            self.assertEqual(self.client.get("/static/nada.js").status_code, 404)
            # O admin manda rota desconhecida para o login dele, nunca para o app.
            response = self.client.get("/admin/nada/")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith("/admin/login/"))

    def test_api_without_slash_still_redirects(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/api/health/")

    def test_post_not_allowed(self):
        (self.dist / "index.html").write_text("<html></html>")
        with override_settings(FRONTEND_DIST=self.dist):
            self.assertEqual(self.client.post("/exames").status_code, 405)


class DatabaseConfigTests(SimpleTestCase):
    def config(self, **environ):
        base = {k: v for k, v in os.environ.items() if not k.startswith("DB_") and k != "DATABASE_URL"}
        with mock.patch.dict(os.environ, {**base, **environ}, clear=True):
            return project_settings._database_config()

    def test_db_vars(self):
        cfg = self.config(DB_HOST="ecg.abc.rds.amazonaws.com", DB_PASSWORD="s3cr3t")
        self.assertEqual(cfg["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual((cfg["HOST"], cfg["PORT"], cfg["NAME"], cfg["USER"], cfg["PASSWORD"]),
                         ("ecg.abc.rds.amazonaws.com", 5432, "ecg", "ecg_admin", "s3cr3t"))
        self.assertEqual(cfg["OPTIONS"], {"sslmode": "require"})
        self.assertEqual((cfg["CONN_MAX_AGE"], cfg["CONN_HEALTH_CHECKS"]), (60, True))

    def test_database_url_wins(self):
        cfg = self.config(DATABASE_URL="postgres://u:p@h:6543/db?sslmode=verify-full", DB_HOST="ignorado")
        self.assertEqual((cfg["HOST"], cfg["PORT"], cfg["NAME"]), ("h", 6543, "db"))
        self.assertEqual(cfg["OPTIONS"]["sslmode"], "verify-full")

    def test_sqlite_fallback(self):
        cfg = self.config()
        self.assertEqual(cfg["ENGINE"], "django.db.backends.sqlite3")
