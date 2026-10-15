"""Smoke test das lambdas mock DEPLOYADAS: todos os cenários e validações.

Sobe uma foto de teste em mock-smoke/ no bucket de imagens, invoca as duas
lambdas em cada cenário e confere status/código contra CONTRATO_LAMBDAS.md.

    python mock_lambdas/smoke_aws.py [--image foto.jpg] [--profile trustedlake]
"""

import argparse
import io
import json
import sys
import uuid

import boto3

IMAGES_BUCKET = "ecg-lambdas-images-785236377332-sa-east-1"
READING, INTERPRETATION = "ecg-reading-mock", "ecg-interpretation-mock"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="foto de ECG (JPG/PNG); sem ela usa uma imagem sintética")
    parser.add_argument("--profile", default="trustedlake")
    parser.add_argument("--region", default="sa-east-1")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3, lam = session.client("s3"), session.client("lambda")
    if args.image:
        body = open(args.image, "rb").read()
    else:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1600, 1000), (255, 235, 235)).save(buf, format="PNG")
        body = buf.getvalue()
    key = f"mock-smoke/{uuid.uuid4()}.img"
    s3.put_object(Bucket=IMAGES_BUCKET, Key=key, Body=body)
    image = {"bucket": IMAGES_BUCKET, "key": key}

    def invoke(function, payload):
        response = lam.invoke(FunctionName=function, Payload=json.dumps(payload).encode())
        if response.get("FunctionError"):
            raise SystemExit(f"FunctionError em {function}: {response['Payload'].read()}")
        return json.loads(response["Payload"].read())

    def read(**extra):
        return invoke(READING, {"request_id": str(uuid.uuid4()), "image": image, "layout": "3x4+1",
                                "calibration": {"speed_mm_s": 25, "gain_mm_mv": 10}, **extra})

    ok = read()
    signal = ok["result"]

    def interpret(**extra):
        payload = {"request_id": str(uuid.uuid4()), "patient": {"sex": "M"}, "signal": signal, **extra}
        return invoke(INTERPRETATION, payload)

    cases = [
        ("leitura success", lambda: ok, "OK", None),
        ("leitura success_with_warnings", lambda: read(mock={"scenario": "success_with_warnings"}), "OK_WITH_WARNINGS", None),
        ("leitura failure", lambda: read(mock={"scenario": "failure"}), "ERROR", "READ_TRACE_EXTRACTION_FAILED"),
        ("leitura failure (grid)", lambda: read(mock={"scenario": "failure", "code": "READ_GRID_NOT_FOUND"}), "ERROR", "READ_GRID_NOT_FOUND"),
        ("leitura refused", lambda: read(mock={"scenario": "refused"}), "REFUSED", "READ_REFUSED_BLURRY"),
        ("leitura refused (cortada)", lambda: read(mock={"scenario": "refused", "code": "READ_REFUSED_CROPPED"}), "REFUSED", "READ_REFUSED_CROPPED"),
        ("leitura layout inválido", lambda: read(layout="5x5"), "ERROR", "LAYOUT_UNSUPPORTED"),
        ("leitura sem imagem", lambda: read(image=None), "ERROR", "IMAGE_MISSING"),
        ("leitura imagem inexistente", lambda: read(image={"bucket": IMAGES_BUCKET, "key": "mock-smoke/nao-existe.jpg"}), "ERROR", "IMAGE_NOT_FOUND"),
        ("leitura calibração inválida", lambda: read(calibration={"speed_mm_s": 30}), "ERROR", "CALIBRATION_INVALID"),
        ("interpretação success", lambda: interpret(), "OK", None),
        ("interpretação success_with_warnings", lambda: interpret(mock={"scenario": "success_with_warnings"}), "OK_WITH_WARNINGS", None),
        ("interpretação failure", lambda: interpret(mock={"scenario": "failure"}), "ERROR", "INTERP_NO_QRS_DETECTED"),
        ("interpretação failure (ruído)", lambda: interpret(mock={"scenario": "failure", "code": "INTERP_SIGNAL_TOO_NOISY"}), "ERROR", "INTERP_SIGNAL_TOO_NOISY"),
        ("interpretação sem sexo", lambda: interpret(patient={}), "ERROR", "SEX_MISSING"),
        ("interpretação sexo inválido", lambda: interpret(patient={"sex": "X"}), "ERROR", "SEX_INVALID"),
        ("interpretação sem sinal", lambda: interpret(signal=None), "ERROR", "SIGNAL_MISSING"),
    ]
    failures = 0
    for name, call, status, code in cases:
        response = call()
        got_code = (response.get("error") or {}).get("code")
        passed = response.get("status") == status and got_code == code
        failures += not passed
        print(f"{'PASS' if passed else 'FAIL'}  {name:38} {response.get('status'):17} {got_code or '':30} "
              f"{response['meta']['duration_ms']} ms")

    overlay = signal["artifacts"]["overlay"]
    url = s3.generate_presigned_url("get_object", Params={"Bucket": overlay["bucket"], "Key": overlay["key"]},
                                    ExpiresIn=3600)
    print(f"\noverlay (1h): {url}")
    try:
        s3.delete_object(Bucket=IMAGES_BUCKET, Key=key)
    except Exception as exc:  # limpeza é best-effort
        print(f"aviso: não removi {key}: {exc}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
