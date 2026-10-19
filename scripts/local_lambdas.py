"""Leitura falsa no contrato 2.0 para o backend local (scripts/local_server.py).

A `ecg-reading` de verdade é uma imagem de container (ecg-analyser) e não roda aqui. Este fake
segue o envelope 2.0 (seis métodos, `files.{json,image,leads}`, avisos por método, sem `warnings`
no topo) e os cenários do campo `mock`: success, success_with_warnings, refused, failure. O sinal
é sintético (72 bpm); a prévia é a própria foto com o traçado desenhado por cima.

Catálogo de erros/avisos: `ecg_contract` do repo tl-gmiler/ecg-lambdas-v1 (no sys.path).
"""

import io
import json
import math
import os
import uuid

from ecg_contract import ContractError, Envelope, validate_common, warning

ENGINE = "local-reading-fake"
METHODS = ("baseline_peaks", "chromatic_ink", "faint_ink", "uneven_photo", "grayscale_contrast", "same_appearance")
SCENARIOS = ("success", "success_with_warnings", "refused", "failure")
SPEEDS = (12.5, 25, 50)
GAINS = (5, 10, 20)
RATE_HZ = 250.0
RR_S = 60 / 72

# (nome, linha, coluna, início em s, duração em s); a tira de ritmo do 3x4+1 sai como II, kind rhythm.
LAYOUTS = {
    "3x4+1": [(name, row, col, col * 2.5, 2.5)
              for row, names in enumerate((("I", "aVR", "V1", "V4"), ("II", "aVL", "V2", "V5"),
                                           ("III", "aVF", "V3", "V6")))
              for col, name in enumerate(names)] + [("II", 3, 0, 0.0, 10.0)],
    "12x1": [(name, row, 0, 0.0, 10.0) for row, name in enumerate(
        ("I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"))],
}
AMPLITUDE = {"I": 0.6, "II": 1.0, "III": 0.5, "aVR": -0.7, "aVL": 0.3, "aVF": 0.7,
             "V1": -0.5, "V2": 0.8, "V3": 1.0, "V4": 1.3, "V5": 1.2, "V6": 0.9}


def _beat(t):
    """Um batimento em mV (ondas P, QRS e T como gaussianas)."""
    def g(center, width, height):
        return height * math.exp(-((t - center) / width) ** 2)
    return g(0.12, 0.03, 0.12) + g(0.24, 0.008, -0.1) + g(0.26, 0.011, 1.0) + g(0.28, 0.008, -0.2) + g(0.5, 0.05, 0.25)


def _samples(name, start_s, duration_s):
    amp = AMPLITUDE[name]
    return [round(amp * _beat((start_s + i / RATE_HZ) % RR_S), 4) for i in range(int(duration_s * RATE_HZ))]


def _validate(event, s3):
    mock = event.get("mock") or {}
    scenario = mock.get("scenario", "success") if isinstance(mock, dict) else None
    if scenario not in SCENARIOS:
        raise ContractError("MOCK_SCENARIO_INVALID", details={"received": mock})
    layout = event.get("layout")
    if not layout:
        raise ContractError("LAYOUT_MISSING")
    if layout not in LAYOUTS:
        raise ContractError("LAYOUT_UNSUPPORTED", details={"received": layout, "allowed": list(LAYOUTS)})
    calibration = event.get("calibration")
    if not (isinstance(calibration, dict) and calibration.get("speed_mm_s") in SPEEDS
            and calibration.get("gain_mm_mv") in GAINS):
        raise ContractError("CALIBRATION_INVALID", details={"received": calibration})
    image = event.get("image")
    if not (isinstance(image, dict) and image.get("bucket") and image.get("key")):
        raise ContractError("IMAGE_MISSING")
    try:
        body = s3.get_object(Bucket=image["bucket"], Key=image["key"])["Body"].read()
    except Exception:
        raise ContractError("IMAGE_NOT_FOUND") from None
    from PIL import Image

    try:
        photo = Image.open(io.BytesIO(body))
        photo.load()
    except Exception:
        raise ContractError("IMAGE_UNREADABLE") from None
    return scenario, layout, calibration, photo.convert("RGB")


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _preview(photo, layout, leads):
    """Foto reduzida com a caixa e o traçado de cada derivação desenhados por cima."""
    from PIL import ImageDraw

    image = photo.copy()
    image.thumbnail((1400, 1400))
    draw = ImageDraw.Draw(image)
    width, height = image.size
    rows = max(lead["row"] for lead in leads) + 1
    cols = 4 if layout == "3x4+1" else 1
    margin, cell_h = width * 0.03, (height * 0.94) / rows
    for lead in leads:
        span = cols if lead["kind"] == "rhythm" else 1
        cell_w = (width - 2 * margin) / cols
        x0, y0 = margin + lead["col"] * cell_w, height * 0.03 + lead["row"] * cell_h
        x1, baseline = x0 + cell_w * span, y0 + cell_h * 0.6
        lead["bbox_px"] = [round(x0), round(y0), round(x1), round(y0 + cell_h)]
        lead["baseline_y_px"] = round(baseline, 1)
        draw.rectangle(lead["bbox_px"], outline=(40, 120, 220), width=2)
        samples = lead["samples"]
        step = max(1, len(samples) // 600)
        points = [(x0 + (x1 - x0) * i / len(samples), baseline - v * cell_h * 0.35)
                  for i, v in enumerate(samples) if i % step == 0]
        draw.line(points, fill=(220, 30, 60), width=2)
        draw.text((x0 + 4, y0 + 2), lead["name"], fill=(40, 120, 220))
    return image


def _put(s3, key, body, content_type):
    bucket = os.environ["RESULTS_BUCKET"]
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return {"bucket": bucket, "key": key, "content_type": content_type}


def _method(name, status, score=None, warnings=None, files=None, reason=None):
    return {"method": name, "version": f"{name}-local", "status": status, "reason": reason,
            "selector_score": score, "sampling_rate_hz": RATE_HZ if status == "OK" else None,
            "warnings": warnings or [], "files": files or {"json": None, "image": None, "leads": None}}


def handler(event, s3):
    env = Envelope("read", ENGINE, "dev")
    try:
        env.request_id = validate_common(event, operation="read")
        scenario, layout, calibration, photo = _validate(event, s3)
        env.scenario = scenario
        run = f"reading/{env.request_id}/{uuid.uuid4().hex[:8]}"

        if scenario == "failure":
            methods = [_method(name, "ERROR", reason="TRACE_EXTRACTION_FAILED") for name in METHODS]
            raise ContractError("READ_TRACE_EXTRACTION_FAILED", details={"methods": methods})
        if scenario == "refused":
            gray = _png(photo.convert("L").resize((max(1, photo.width // 3), max(1, photo.height // 3))))
            methods = [_method(name, "ABSTAINED", reason="unreadable_trace", files={
                "json": None, "image": _put(s3, f"{run}/{name}/rejection_preview.png", gray, "image/png"),
                "leads": None}) for name in METHODS[:1]] + [_method(name, "ABSTAINED") for name in METHODS[1:]]
            raise ContractError("READ_REFUSED_UNREADABLE_TRACE", details={"methods": methods})

        leads = [{"name": name, "kind": "rhythm" if duration > 2.5 and layout == "3x4+1" else "panel",
                  "row": row, "col": col, "start_s": start, "duration_s": duration,
                  "quality": "good", "coverage": 1.0, "samples": _samples(name, start, duration)}
                 for name, row, col, start, duration in LAYOUTS[layout]]
        warnings = []
        if scenario == "success_with_warnings":
            leads[6]["quality"], leads[6]["coverage"] = "partial", 0.6
            warnings = [warning("AMBIGUOUS_TRACE", lead=leads[6]["name"]), warning("GRID_CALIBRATION_UNCERTAIN")]
        preview = _png(_preview(photo, layout, leads))
        best = "faint_ink"
        files = {
            "json": _put(s3, f"{run}/{best}/result.json", json.dumps({"method": best}).encode(), "application/json"),
            "image": _put(s3, f"{run}/{best}/preview.png", preview, "image/png"),
            "leads": _put(s3, f"{run}/{best}/leads.json",
                          json.dumps({"layout": layout, "units": "mV", "leads": leads}).encode(), "application/json"),
        }
        methods = []
        for index, name in enumerate(METHODS):
            if name == best:
                methods.append(_method(name, "OK", 87.5, warnings, files))
            elif scenario == "success_with_warnings" and index % 2:
                methods.append(_method(name, "ABSTAINED", reason="no_measured_lead"))
            else:
                # Os outros métodos OK reaproveitam os arquivos do escolhido (o backend só lê o melhor).
                methods.append(_method(name, "OK", 40.0 + index, files=files))
        result = {"layout": layout, "calibration": calibration, "units": "mV", "methods": methods}
        response = env._build("OK_WITH_WARNINGS" if warnings else "OK", result=result)
    except ContractError as exc:
        response = env.failure(exc)
    # Contrato 2.0: os avisos ficam em cada método, não no topo do envelope.
    response.pop("warnings", None)
    return response
