"""Smoke E2E ponta-a-ponta contra AWS real (lambdas + S3) e servidor Django.

Fluxo: login -> criar exame -> criar attempt (presigned PUT) -> subir imagem
real -> Fase A (lambda-1 + lambda-2) -> aprovar (lambda-3) -> detail + feedback.
Limpa todos os artefatos (S3 + Neon) ao final.
"""

import json
import os
import time

import boto3
import django
import requests

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from rest_framework.authtoken.models import Token  # noqa: E402

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8001")
EMAIL = "smoke-e2e@test.local"
PASS = "smoke-e2e-senha-1"
SESSION = requests.Session()
User = get_user_model()
S3 = boto3.Session(profile_name=os.environ.get("AWS_PROFILE") or None, region_name=settings.AWS_REGION).client("s3")


def api(method, path, **kw):
    kw.setdefault("timeout", (30, 900))
    r = SESSION.request(method, BASE + path, **kw)
    print(f"  HTTP {r.status_code} {method} {path}")
    return r


def cleanup_keys(keys):
    for k in keys:
        try:
            S3.delete_object(Bucket=settings.AWS_IMAGES_BUCKET, Key=k)
        except Exception:
            pass
        try:
            S3.delete_object(Bucket=settings.AWS_RESULTS_BUCKET, Key=k)
        except Exception:
            pass


def main():
    # usu??rio tempor??rio
    user = User.objects.create_user(email=EMAIL, password=PASS, name="Smoke E2E", crm="CRM-E2E")
    token = Token.objects.create(user=user)
    SESSION.headers["Authorization"] = f"Token {token.key}"

    to_delete_keys = []
    try:
        # 1. login real
        r = api("POST", "/api/auth/login/", json={"email": EMAIL, "password": PASS})
        assert r.status_code == 200, r.text

        # 2. exame com paciente inline
        r = api("POST", "/api/exams/", json={"name": "Paciente Smoke", "age": 44, "gender": "M", "reason": "e2e"})
        assert r.status_code == 201, r.text
        exam = r.json()
        exam_id = exam["id"]
        print(f"  exam_id={exam_id} status={exam['status']}")

        # 3. attempt -> presigned PUT
        r = api("POST", f"/api/exams/{exam_id}/attempts/", json={"filename": "Screenshot 2026-06-07 at 20.04.17.png"})
        assert r.status_code == 201, r.text
        body = r.json()
        attempt_id = body["attempt_id"]
        upload_url = body["upload_url"]
        s3_key = body["imagem_s3_key"]
        to_delete_keys.append(s3_key)

        # 4. baixar a imagem de teste do S3 e subir via presigned PUT (sem passar pelo Django)
        #    IMPORTANTE: a chamada NÃO pode carregar o header Authorization (Token) — S3 rejeita.
        img = S3.get_object(Bucket=settings.AWS_IMAGES_BUCKET, Key="test_images/Screenshot 2026-06-07 at 20.04.17.png")["Body"].read()
        r = requests.put(upload_url, data=img, timeout=(30, 300))
        assert r.status_code == 200, f"presigned PUT falhou: {r.status_code} {r.text}"
        print(f"  imagem {s3_key} enviada via presigned PUT ({len(img)} bytes)")

        # 5. Fase A (lambda-1 + lambda-2) ??? s??ncrona, pode demorar
        t0 = time.time()
        r = api("POST", f"/api/exams/{exam_id}/attempts/{attempt_id}/process/")
        print(f"  Fase A levou {time.time() - t0:.1f}s")
        assert r.status_code == 200, r.text
        pa = r.json()
        assert pa["status"] == "aguardando_aprovacao", pa
        print(f"  Fase A ok: image_type={pa['image_type']} fidelidade={pa['fidelidade']}")
        to_delete_keys.append(f"results/{exam_id}/trace_mask.npz")
        to_delete_keys.append(f"results/{exam_id}/overlay_trace.png")
        to_delete_keys.append(f"results/{exam_id}/overlay_signal.png")

        # 6. confirmar que o overlay é baixável (GET sem Authorization, como <img>)
        ov = requests.get(pa["overlay_trace_url"], timeout=60)
        print(f"  overlay_trace GET: {ov.status_code} ({len(ov.content)} bytes)")
        assert ov.status_code == 200

        # 7. Fase B — lambda-3
        t0 = time.time()
        r = api("POST", f"/api/exams/{exam_id}/attempts/{attempt_id}/aprovar/")
        print(f"  Fase B levou {time.time() - t0:.1f}s")
        assert r.status_code == 200, r.text
        report = r.json()
        print(f"  Report: ritmo={report.get('ritmo', {}).get('ritmo')!r} "
              f"fc={report.get('ritmo', {}).get('fc_bpm')} eixo={report.get('eixo_graus')} "
              f"confiabilidade={report.get('confiabilidade')} versao={report.get('versao_codigo')}")
        print(f"  conclusao: {report.get('conclusao')}")
        assert all(k in report for k in ["imagem_original_url", "overlay_trace_url", "overlay_signal_url"])

        # 8. detail do exame
        r = api("GET", f"/api/exams/{exam_id}")
        assert r.status_code == 200, r.text
        det = r.json()
        attempts = det["attempts"]
        print(f"  detail: status={det['status']} attempts={len(attempts)}")
        concluded = [a for a in attempts if a["status"] == "concluido"]
        assert concluded and concluded[0]["report"], "report ausente na detail"
        print("  E2E RODOU AT?? O FIM COM LAMBDAS REAIS")
    finally:
        cleanup_keys(to_delete_keys)
        Token.objects.filter(user=user).delete()
        user.delete()
        assert not User.objects.filter(email=EMAIL).exists()
        print("  limpeza feita")


if __name__ == "__main__":
    main()
