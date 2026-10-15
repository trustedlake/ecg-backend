"""Lambda MOCK de leitura de ECG (contrato v1, operation=read).

Valida a entrada de verdade (layout, calibração, imagem no S3), mas o sinal é
sintético: o traço é desenhado sobre a foto original junto com a grade, para
exercitar a validação visual do app. O cenário é escolhido por `mock.scenario`
no payload ou pela env MOCK_SCENARIO. Ver CONTRATO_LAMBDAS.md.
"""

import io
import json
import logging
import math
import os
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from ecg_contract import (
    GAINS_MM_MV, LAYOUTS, PAPER_SECONDS, READ_FAILURE_CODES, READ_REFUSAL_CODES,
    SCENARIO_FAILURE, SCENARIO_REFUSED, SCENARIO_SUCCESS, SCENARIO_WARNINGS,
    SPEEDS_MM_S, ContractError, Envelope, resolve_mock, validate_common, warning,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENGINE = "ecg-reading-mock"
ENGINE_VERSION = os.environ.get("CODE_VERSION", "dev")
SAMPLING_RATE_HZ = 500
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MIN_LONG_SIDE_PX, MIN_SHORT_SIDE_PX = 800, 500
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
DEFAULT_CALIBRATION = {"speed_mm_s": 25.0, "gain_mm_mv": 10.0}
HEART_RATE_BPM = 72

SCENARIOS = (SCENARIO_SUCCESS, SCENARIO_WARNINGS, SCENARIO_FAILURE, SCENARIO_REFUSED)
SCENARIO_CODES = {SCENARIO_FAILURE: READ_FAILURE_CODES, SCENARIO_REFUSED: READ_REFUSAL_CODES}

# Morfologia sintética por derivação: (escala do QRS, escala da onda T, fração positiva do QRS).
LEAD_SHAPE = {
    "I": (0.8, 0.25, 0.9), "II": (1.2, 0.35, 0.9), "III": (0.5, 0.15, 0.8),
    "aVR": (-0.9, -0.25, 0.9), "aVL": (0.4, 0.12, 0.7), "aVF": (0.8, 0.25, 0.85),
    "V1": (0.9, 0.1, 0.2), "V2": (1.4, 0.35, 0.35), "V3": (1.5, 0.45, 0.55),
    "V4": (1.7, 0.45, 0.8), "V5": (1.6, 0.4, 0.9), "V6": (1.3, 0.3, 0.95),
}

s3 = None


def _s3():
    global s3
    if s3 is None:
        s3 = boto3.client("s3", config=Config(retries={"max_attempts": 3}))
    return s3


def handler(event, context):
    env = Envelope("read", ENGINE, ENGINE_VERSION)
    try:
        env.request_id = validate_common(event)
        scenario, code = resolve_mock(event, os.environ.get("MOCK_SCENARIO"), SCENARIOS, SCENARIO_CODES)
        env.scenario = scenario
        layout = _validate_layout(event.get("layout"))
        calibration = _validate_calibration(event.get("calibration"))
        bucket, key = _validate_image_ref(event.get("image"))
        image = _load_image(bucket, key)
        response = _run(env, scenario, code, image, layout, calibration)
    except ContractError as exc:
        logger.info("read %s: %s", env.request_id, exc)
        response = env.failure(exc)
    except Exception:  # nunca vazar FunctionError para o backend
        logger.exception("read %s: erro inesperado", env.request_id)
        response = env.failure(ContractError("INTERNAL_ERROR"))
    logger.info(json.dumps({"request_id": response["request_id"], "status": response["status"],
                            "error": (response["error"] or {}).get("code"),
                            "scenario": env.scenario, "duration_ms": response["meta"]["duration_ms"]}))
    return response


# ─── Validação ────────────────────────────────────────────────────────────────

def _validate_layout(layout):
    if layout in (None, ""):
        raise ContractError("LAYOUT_MISSING")
    if layout not in LAYOUTS:
        raise ContractError("LAYOUT_UNSUPPORTED", details={"received": layout, "supported": list(LAYOUTS)})
    return layout


def _validate_calibration(calibration):
    if calibration is None:
        return dict(DEFAULT_CALIBRATION)
    if not isinstance(calibration, dict):
        raise ContractError("CALIBRATION_INVALID", details={"received": calibration})
    result = {}
    for field, allowed in (("speed_mm_s", SPEEDS_MM_S), ("gain_mm_mv", GAINS_MM_MV)):
        value = calibration.get(field, DEFAULT_CALIBRATION[field])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) not in allowed:
            raise ContractError("CALIBRATION_INVALID", details={"field": field, "received": value,
                                                                "allowed": list(allowed)})
        result[field] = float(value)
    return result


def _validate_image_ref(image):
    if not isinstance(image, dict):
        raise ContractError("IMAGE_MISSING")
    bucket, key = image.get("bucket"), image.get("key")
    if not (isinstance(bucket, str) and bucket and isinstance(key, str) and key):
        raise ContractError("IMAGE_MISSING")
    return bucket, key


def _load_image(bucket, key):
    try:
        head = _s3().head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] > MAX_IMAGE_BYTES:
            raise ContractError("IMAGE_TOO_LARGE", details={"bytes": head["ContentLength"],
                                                            "max_bytes": MAX_IMAGE_BYTES})
        body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "403", "NoSuchKey", "NotFound", "AccessDenied"):
            raise ContractError("IMAGE_NOT_FOUND", details={"bucket": bucket, "key": key}) from exc
        raise ContractError("STORAGE_ERROR") from exc
    except BotoCoreError as exc:
        raise ContractError("STORAGE_ERROR") from exc
    try:
        image = Image.open(io.BytesIO(body))
        fmt = image.format
        if fmt not in FORMATS:
            raise ContractError("IMAGE_FORMAT_UNSUPPORTED", details={"detected": fmt,
                                                                     "supported": list(FORMATS.values())})
        image = ImageOps.exif_transpose(image).convert("RGB")
    except ContractError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ContractError("IMAGE_UNREADABLE") from exc
    width, height = image.size
    if max(width, height) < MIN_LONG_SIDE_PX or min(width, height) < MIN_SHORT_SIDE_PX:
        raise ContractError("IMAGE_TOO_SMALL", details={"width": width, "height": height})
    return image


# ─── Cenários ─────────────────────────────────────────────────────────────────

def _run(env, scenario, code, image, layout, calibration):
    prefix = f"reading/{env.request_id}/{uuid.uuid4().hex}/"
    if scenario == SCENARIO_FAILURE:
        raise ContractError(code or "READ_TRACE_EXTRACTION_FAILED")
    if scenario == SCENARIO_REFUSED:
        raise _refusal(image, code or "READ_REFUSED_BLURRY", prefix)

    geometry = _geometry(image.size, layout, calibration)
    leads = _synthesize(layout, geometry, calibration)
    warnings = []
    if scenario == SCENARIO_WARNINGS:
        warnings = _degrade(leads, geometry)
    overlay = _draw_overlay(image, geometry, leads)
    result = {
        "layout": layout,
        "calibration": calibration,
        "sampling_rate_hz": SAMPLING_RATE_HZ,
        "units": "mV",
        "grid": geometry["grid"],
        "leads": [{k: v for k, v in lead.items() if not k.startswith("_")} for lead in leads],
    }
    artifacts = {
        "overlay": _put(prefix + "overlay.png", _png(overlay), "image/png"),
    }
    artifacts["signal"] = _put(prefix + "signal.json", json.dumps(result).encode(), "application/json")
    result["artifacts"] = artifacts
    return env.success(result, warnings)


def _refusal(image, code, prefix):
    width, height = image.size
    regions = {
        "READ_REFUSED_CROPPED": (int(width * 0.85), 0, width, height),
        "READ_REFUSED_OCCLUDED": (int(width * 0.35), int(height * 0.3), int(width * 0.6), int(height * 0.7)),
    }
    region = regions.get(code, (0, 0, width, height))
    preview = image.copy()
    draw = ImageDraw.Draw(preview, "RGBA")
    stroke = max(4, width // 200)
    draw.rectangle(region, fill=(220, 30, 30, 60), outline=(220, 30, 30, 255), width=stroke)
    artifact = _put(prefix + "refusal_preview.png", _png(preview), "image/png")
    return ContractError(code, details={"region_px": list(region), "preview": artifact})


def _degrade(leads, geometry):
    """Cenário com avisos: um trecho sem leitura, uma derivação saturada e rotação corrigida."""
    geometry["grid"]["rotation_deg"] = 1.8
    warnings = [warning("IMAGE_ROTATION_CORRECTED")]
    by_name = {lead["name"]: lead for lead in leads if lead["kind"] == "panel"}
    partial = by_name.get("V2") or leads[0]
    samples = partial["samples"]
    start, end = int(len(samples) * 0.3), int(len(samples) * 0.7)
    samples[start:end] = [None] * (end - start)
    partial["quality"], partial["coverage"] = "partial", round(1 - (end - start) / len(samples), 3)
    warnings.append(warning("LEAD_PARTIAL_TRACE", lead=partial["name"]))
    saturated = by_name.get("aVL") or leads[-1]
    if saturated is not partial:
        limit = 0.3
        saturated["samples"] = [None if v is None else max(-limit, min(limit, v)) for v in saturated["samples"]]
        saturated["quality"] = "saturated"
        warnings.append(warning("LEAD_SATURATED", lead=saturated["name"]))
    return warnings


# ─── Geometria e sinal sintético ──────────────────────────────────────────────

def _geometry(size, layout, calibration):
    width, height = size
    grid_rows, rhythm = LAYOUTS[layout]
    x0, x1 = width * 0.04, width * 0.96
    y0, y1 = height * 0.06, height * 0.96
    paper_mm = PAPER_SECONDS * calibration["speed_mm_s"]
    small = (x1 - x0) / paper_mm
    row_h = (y1 - y0) / (len(grid_rows) + len(rhythm))
    return {
        "x0": x0, "x1": x1, "y0": y0, "row_h": row_h, "small": small,
        "px_per_mv": small * calibration["gain_mm_mv"],
        "grid": {"small_box_px": round(small, 3), "large_box_px": round(small * 5, 3),
                 "origin_px": [round(x0, 1), round(y0, 1)], "rotation_deg": 0.0,
                 "paper_bbox_px": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]},
    }


def _beat(t):
    """Batimento sintético (mV) em torno do pico R em t=0: soma de gaussianas P-Q-R-S-T."""
    def g(amp, center, width):
        return amp * math.exp(-((t - center) ** 2) / (2 * width ** 2))
    return (g(0.12, -0.16, 0.022), g(-0.12, -0.028, 0.009), g(1.0, 0.0, 0.011),
            g(-0.35, 0.03, 0.011), g(1.0, 0.28, 0.045))


def _lead_value(name, t):
    qrs_scale, t_scale, positive = LEAD_SHAPE[name]
    rr = 60.0 / HEART_RATE_BPM
    phase = (t - 0.35) % rr
    if phase > rr / 2:
        phase -= rr
    p, q, r, s, tw = _beat(phase)
    qrs = qrs_scale * (positive * r + q + s * (1 + 2 * (1 - positive)))
    return round(p * (1 if qrs_scale > 0 else -1) + qrs + t_scale * tw, 4) + 0.0  # sem -0.0


def _synthesize(layout, geometry, calibration):
    grid_rows, rhythm = LAYOUTS[layout]
    px_per_s = geometry["small"] * calibration["speed_mm_s"]
    leads = []
    rows = [(row, cols) for row, cols in enumerate(grid_rows)]
    rows += [(len(grid_rows) + i, (name,)) for i, name in enumerate(rhythm)]
    for row, cols in rows:
        is_rhythm = row >= len(grid_rows)
        seg = PAPER_SECONDS / len(cols)
        baseline = geometry["y0"] + geometry["row_h"] * (row + 0.5)
        for col, name in enumerate(cols):
            start = col * seg
            n = int(round(seg * SAMPLING_RATE_HZ))
            samples = [_lead_value(name, start + i / SAMPLING_RATE_HZ) for i in range(n)]
            x_start = geometry["x0"] + start * px_per_s
            leads.append({
                "name": name, "kind": "rhythm" if is_rhythm else "panel", "row": row, "col": col,
                "start_s": round(start, 3), "duration_s": round(seg, 3),
                "quality": "good", "coverage": 1.0,
                "bbox_px": [round(x_start, 1), round(baseline - geometry["row_h"] / 2, 1),
                            round(x_start + seg * px_per_s, 1), round(baseline + geometry["row_h"] / 2, 1)],
                "baseline_y_px": round(baseline, 1),
                "samples": samples,
                "_x_start": x_start,
            })
    return leads


# ─── Overlay ──────────────────────────────────────────────────────────────────

def _draw_overlay(image, geometry, leads):
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay, "RGBA")
    width, height = overlay.size
    small = geometry["small"]
    x0, y0 = geometry["x0"], geometry["y0"]
    x1, y1 = geometry["grid"]["paper_bbox_px"][2], geometry["grid"]["paper_bbox_px"][3]
    thin, thick = max(1, int(small / 8)), max(2, int(small / 4))
    i = 0
    while x0 + i * small <= x1 + 0.5:
        x = x0 + i * small
        major = i % 5 == 0
        draw.line([(x, y0), (x, y1)], fill=(0, 170, 0, 150 if major else 60), width=thick if major else thin)
        i += 1
    i = 0
    while y0 + i * small <= y1 + 0.5:
        y = y0 + i * small
        major = i % 5 == 0
        draw.line([(x0, y), (x1, y)], fill=(0, 170, 0, 150 if major else 60), width=thick if major else thin)
        i += 1

    font = ImageFont.load_default(size=max(14, height // 40))
    trace_width = max(2, width // 500)
    px_per_sample = (x1 - x0) / (PAPER_SECONDS * SAMPLING_RATE_HZ)
    for lead in leads:
        px_per_mv = geometry["px_per_mv"]
        segment = []
        for idx, value in enumerate(lead["samples"]):
            if value is None:
                if len(segment) > 1:
                    draw.line(segment, fill=(20, 60, 230, 255), width=trace_width, joint="curve")
                segment = []
                continue
            segment.append((lead["_x_start"] + idx * px_per_sample, lead["baseline_y_px"] - value * px_per_mv))
        if len(segment) > 1:
            draw.line(segment, fill=(20, 60, 230, 255), width=trace_width, joint="curve")
        bx0, by0, bx1, by1 = lead["bbox_px"]
        draw.rectangle([bx0, by0, bx1, by1], outline=(230, 120, 0, 200), width=max(1, thin))
        draw.text((bx0 + 6, by0 + 4), lead["name"], fill=(230, 120, 0, 255), font=font)
    draw.text((x0, max(2, y0 - font.size - 6)), "MOCK — sinal sintético, não é leitura real",
              fill=(200, 0, 0, 255), font=font)
    return overlay


# ─── S3 ───────────────────────────────────────────────────────────────────────

def _png(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _put(key, body, content_type):
    bucket = os.environ["RESULTS_BUCKET"]
    try:
        _s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    except (ClientError, BotoCoreError) as exc:
        raise ContractError("STORAGE_ERROR") from exc
    return {"bucket": bucket, "key": key, "content_type": content_type}
