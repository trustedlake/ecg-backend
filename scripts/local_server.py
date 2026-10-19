"""Backend local completo, sem AWS: Django + lambdas falsas no mesmo processo + S3 falso em disco.

Para desenvolver e conferir o app sem gravar nada na AWS:
- `services._lambda_client()` vira um cliente falso: a leitura é `scripts/local_lambdas.py` (contrato
  2.0, cenários do campo `mock`) e a interpretação é o handler mock de verdade do repo
  tl-gmiler/ecg-lambdas-v1 (`mock_lambdas/interpretation/handler.py`), lido de `--lambdas-repo`;
- o S3 (presigned PUT/GET, artefatos das lambdas, sinal) vira uma pasta local servida em `/__local_s3/`;
- `ECG_TASK_MODE=inline`, cenários de teste ligados e um usuário staff `dev` / `dev12345`;
- `--async N`: imita a fila de produção. O endpoint responde 202 na hora e a fase roda numa thread
  depois de N segundos (o app faz polling, como com o worker SQS).

Uso (venv com os requirements do backend + Pillow + numpy):
    python scripts/local_server.py [--port 23132] [--data /tmp/ecg-local] [--async 3]
                                   [--lambdas-repo ../ecg-lambdas-v1]
Se existir `frontend_dist/` (export web do Expo), o app web abre na raiz, mesma origem da API.
"""

import argparse
import importlib.util
import io
import json
import mimetypes
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=23132)
    parser.add_argument("--data", default="/tmp/ecg-local", help="pasta do SQLite e do S3 falso")
    parser.add_argument("--async", dest="async_delay", type=float, default=None, metavar="SEGUNDOS",
                        help="roda as fases numa thread após SEGUNDOS (imita a fila SQS)")
    parser.add_argument("--lambdas-repo", default=os.environ.get("ECG_LAMBDAS_V1_DIR", str(ROOT.parent / "ecg-lambdas-v1")),
                        help="clone do tl-gmiler/ecg-lambdas-v1 (interpretação mock e ecg_contract)")
    return parser.parse_args()


ARGS = parse_args()
LAMBDAS = Path(ARGS.lambdas_repo).resolve() / "mock_lambdas"
if not (LAMBDAS / "interpretation" / "handler.py").is_file():
    sys.exit(f"Não achei {LAMBDAS}/interpretation/handler.py; clone o tl-gmiler/ecg-lambdas-v1 e use --lambdas-repo.")
DATA = Path(ARGS.data)
S3_DIR = DATA / "s3"
BASE_URL = f"http://127.0.0.1:{ARGS.port}"

os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DATA / 'db.sqlite3'}")
os.environ.setdefault("ALLOWED_HOSTS", "127.0.0.1,localhost")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:8081,http://127.0.0.1:8081")
os.environ["ECG_TASK_MODE"] = "inline"
os.environ.setdefault("ECG_TEST_SCENARIOS_ENABLED", "True")
os.environ.setdefault("RESULTS_BUCKET", "ecg-lambdas-results-785236377332-sa-east-1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path[:0] = [str(ROOT), str(HERE), str(LAMBDAS)]
S3_DIR.mkdir(parents=True, exist_ok=True)


def _client_error(code, operation):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _path(bucket, key):
    path = (S3_DIR / bucket / key).resolve()
    if not str(path).startswith(str(S3_DIR.resolve()) + os.sep):
        raise ValueError("chave fora do S3 local")
    return path


class FakeS3:
    """Subconjunto do cliente S3 usado pelas lambdas mock e pelo backend."""

    def put_object(self, Bucket, Key, Body, ContentType=None, **_):
        path = _path(Bucket, Key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Body if isinstance(Body, bytes) else Body.encode())
        if ContentType:
            path.with_name(path.name + ".content-type").write_text(ContentType)
        return {}

    def head_object(self, Bucket, Key):
        path = _path(Bucket, Key)
        if not path.is_file():
            raise _client_error("404", "HeadObject")
        return {"ContentLength": path.stat().st_size}

    def get_object(self, Bucket, Key):
        path = _path(Bucket, Key)
        if not path.is_file():
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(path.read_bytes())}


def _load_handler(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLambda:
    """Chama os handlers no processo; mesmo formato de resposta do boto3 `invoke`."""

    def __init__(self, s3):
        import local_lambdas

        self.reading = lambda event, _context: local_lambdas.handler(event, s3)
        self.interpretation = _load_handler("mock_interpretation_handler",
                                            LAMBDAS / "interpretation" / "handler.py").handler

    def invoke(self, FunctionName, Payload, **_):
        from django.conf import settings

        handler = {
            settings.AWS_LAMBDA_ECG_READING: self.reading,
            settings.AWS_LAMBDA_ECG_INTERPRETATION: self.interpretation,
        }[FunctionName]
        result = handler(json.loads(Payload), None)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode())}


def main():
    import django

    django.setup()

    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.http import FileResponse, HttpResponse, HttpResponseNotFound
    from django.urls import path
    from django.views.decorators.csrf import csrf_exempt

    from config import urls
    from exams import services, tasks

    s3 = FakeS3()
    fake_lambda = FakeLambda(s3)
    services._lambda_client = lambda: fake_lambda
    services._s3_client = lambda: s3
    services.presign_put_url = lambda bucket, key, expires_in, content_type=None: (
        f"{BASE_URL}/__local_s3/{bucket}/{key}")
    services.presign_get_url = lambda bucket, key, expires_in: f"{BASE_URL}/__local_s3/{bucket}/{key}"

    if ARGS.async_delay is not None:
        import threading
        import time

        from django.db import connection

        def dispatch_async(task, attempt):
            def worker():
                time.sleep(ARGS.async_delay)
                try:
                    tasks.run_task(task, str(attempt.pk))
                finally:
                    connection.close()

            threading.Thread(target=worker, daemon=True).start()

        tasks.dispatch = dispatch_async

    @csrf_exempt
    def local_s3(request, bucket, key):
        try:
            file_path = _path(bucket, key)
        except ValueError:
            return HttpResponseNotFound()
        if request.method == "PUT":
            s3.put_object(bucket, key, request.body, request.headers.get("Content-Type"))
            response = HttpResponse(status=200)
        elif request.method in ("GET", "HEAD") and file_path.is_file():
            type_file = file_path.with_name(file_path.name + ".content-type")
            content_type = (type_file.read_text() if type_file.is_file()
                            else mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            response = FileResponse(file_path.open("rb"), content_type=content_type)
        elif request.method == "OPTIONS":
            response = HttpResponse(status=200)
        else:
            return HttpResponseNotFound()
        # O Expo dev server (porta 8081) sobe a foto direto aqui, como faria no S3.
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "GET, PUT, HEAD, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    urls.urlpatterns.insert(0, path("__local_s3/<str:bucket>/<path:key>", local_s3))

    call_command("migrate", verbosity=0)
    User = get_user_model()
    if not User.objects.filter(username="dev").exists():
        User.objects.create_superuser(username="dev", password="dev12345", email="dev@local", name="Dra. Dev")
        print("Usuário criado: dev / dev12345 (staff)")
    mode = "inline" if ARGS.async_delay is None else f"assíncrono, {ARGS.async_delay:g} s por fase"
    print(f"Backend local (lambdas falsas + S3 em {S3_DIR}, fases {mode}) em {BASE_URL}")
    call_command("runserver", f"127.0.0.1:{ARGS.port}", use_reloader=False)


if __name__ == "__main__":
    main()
