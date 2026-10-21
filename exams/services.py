"""Django integration with AWS S3 and the ECG lambdas (tl-gmiler/ecg-lambdas-v1, lambda contract).

Reading: `ecg-reading`, contract 2.0 (six methods; the backend keeps the `OK` one with the highest
`selector_score`). Interpretation: `ecg-interpretation` (mock), which receives as `signal` the
`leads.json` of the chosen method. Test scenarios are the `mock` field of the payload.

- `invoke_contract` always returns a contract envelope (an infrastructure failure becomes
  a synthetic retryable `ERROR`/`INTERNAL_ERROR` envelope, never an exception).
- `run_reading` / `run_interpretation` run one phase and store the outcome on the attempt
  (states in docs/user-flow-v1.md §3). They are synchronous: whether they run inline or
  in the worker is decided by `exams/tasks.py` (step B3).
- `attempt_reading_view` / `attempt_interpretation_view` / `attempt_flags` build the blocks
  of the API `Attempt` object (plan §0.1); presigned URLs are generated on the fly, never stored.
"""

import json
import logging
from datetime import timedelta

import boto3
from botocore.config import Config

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Classification, Exam, ExamAttempt, ExamStatus, Report

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "2.0"

STATUS_OK = "OK"
STATUS_OK_WITH_WARNINGS = "OK_WITH_WARNINGS"
STATUS_REFUSED = "REFUSED"
STATUS_ERROR = "ERROR"
SUCCESS_STATUSES = (STATUS_OK, STATUS_OK_WITH_WARNINGS)
READ_STATUSES = (STATUS_OK, STATUS_OK_WITH_WARNINGS, STATUS_REFUSED, STATUS_ERROR)
INTERPRET_STATUSES = (STATUS_OK, STATUS_OK_WITH_WARNINGS, STATUS_ERROR)
METHOD_OK = "OK"

# Errors the backend itself generates (no valid lambda response). Same codes as the catalog.
LOCAL_ERRORS = {
    "INTERNAL_ERROR": ("internal", True, "Processing could not be completed right now. Try again in a moment."),
    "STORAGE_ERROR": ("internal", True, "Failed to read the reading artifacts. Try again in a moment."),
    "SEX_MISSING": ("validation", False, "Enter the patient's sex to interpret the ECG."),
    "TASK_TIMEOUT": ("internal", True, "Processing took longer than expected. Try again."),
}

# Warning the backend adds when the lambda says OK_WITH_WARNINGS with no warning on the chosen method
# (in contract 2.0 this means one of the six methods did not finish).
METHODS_PARTIAL_WARNING = {
    "code": "METHODS_PARTIAL_RESULT",
    "message": "Not all reading methods finished; the trace shown is from the most reliable method.",
}

LOG_BODY_LIMIT = 2000


# --------------------------------------------------------------------------- clientes AWS


def _session():
    if settings.AWS_PROFILE_NAME:
        return boto3.Session(
            profile_name=settings.AWS_PROFILE_NAME,
            region_name=settings.AWS_REGION,
        )
    return boto3.Session(region_name=settings.AWS_REGION)


def _lambda_client():
    # Waits for the lambda to finish (up to AWS_LAMBDA_READ_TIMEOUT) and never re-invokes on its own:
    # retrying is up to the user (retry) or the queue.
    return _session().client(
        "lambda",
        config=Config(
            read_timeout=settings.AWS_LAMBDA_READ_TIMEOUT,
            connect_timeout=10,
            retries={"max_attempts": 0},
        ),
    )


def _s3_client():
    """S3 client forced to SigV4 for presigned URLs (SigV2 deprecated by AWS)."""
    return _session().client("s3", config=Config(signature_version="s3v4"))


def presign_put_url(bucket, key, expires_in, content_type=None):
    """Presigned PUT straight from the frontend to S3 (the upload never goes through Django).

    If content_type is given, it is SIGNED into the URL: the client MUST send
    the same value in the Content-Type header, otherwise the signature fails (403).
    """
    params = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return _s3_client().generate_presigned_url(
        "put_object", Params=params, ExpiresIn=expires_in
    )


def presign_get_url(bucket, key, expires_in):
    """On-demand presigned GET: URLs are never stored in the database."""
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def original_image_url(attempt, expires_in):
    if not attempt.image_s3_key:
        return None
    return presign_get_url(settings.AWS_IMAGES_BUCKET, attempt.image_s3_key, expires_in)


def _read_json_from_s3(bucket, key):
    body = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


# --------------------------------------------------------------------------- envelope


def local_error(code, details=None):
    category, retryable, message = LOCAL_ERRORS[code]
    error = {"code": code, "category": category, "retryable": retryable, "message": message}
    if details:
        error["details"] = details
    return error


def _synthetic_envelope(operation, request_id, code, details=None):
    return {
        "contract_version": CONTRACT_VERSION,
        "operation": operation,
        "request_id": request_id,
        "status": STATUS_ERROR,
        "warnings": [],
        "error": local_error(code, details),
        "result": None,
        "meta": {"engine": "ecg-backend", "synthetic": True},
    }


def _is_str(value):
    return isinstance(value, str) and value != ""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _envelope_problem(body, operation, request_id):
    """Returns why the envelope is invalid, or None if it follows contract §1.1."""
    if not isinstance(body, dict):
        return "body is not an object"
    allowed = READ_STATUSES if operation == "read" else INTERPRET_STATUSES
    status = body.get("status")
    if status not in allowed:
        return f"invalid status: {status!r}"
    if body.get("request_id") not in (None, request_id):
        return "request_id differs from the one sent"
    warnings = body.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(w, dict) and _is_str(w.get("code")) and isinstance(w.get("message"), str)
        for w in warnings
    ):
        return "invalid warnings"
    if status in SUCCESS_STATUSES:
        if not isinstance(body.get("result"), dict):
            return "result missing on a success status"
    else:
        error = body.get("error")
        if not (
            isinstance(error, dict)
            and _is_str(error.get("code"))
            and _is_str(error.get("category"))
            and isinstance(error.get("retryable"), bool)
            and isinstance(error.get("message"), str)
        ):
            return "error missing or incomplete"
    return None


def invoke_contract(function_name, payload, operation=None):
    """Invokes the lambda (RequestResponse) and ALWAYS returns a contract v1 envelope.

    FunctionError (timeout/OOM/exception), a boto3/network error or an off-contract envelope become
    a synthetic `ERROR` / `INTERNAL_ERROR` envelope with `retryable: true`.
    """
    operation = operation or ("interpret" if "signal" in payload else "read")
    request_id = payload.get("request_id")
    try:
        response = _lambda_client().invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        raw = response["Payload"].read()
    except Exception:  # read timeout, ClientError, connection, credentials
        logger.exception("Failed to invoke lambda %s (request_id=%s)", function_name, request_id)
        return _synthetic_envelope(operation, request_id, "INTERNAL_ERROR")

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    if response.get("FunctionError"):
        logger.error(
            "Lambda %s FunctionError=%s (request_id=%s): %s",
            function_name, response["FunctionError"], request_id, text[:LOG_BODY_LIMIT],
        )
        return _synthetic_envelope(operation, request_id, "INTERNAL_ERROR")

    try:
        body = json.loads(text)
    except ValueError:
        body = None
    problem = _envelope_problem(body, operation, request_id)
    if problem:
        logger.error(
            "Lambda %s returned an off-contract envelope (%s, request_id=%s): %s",
            function_name, problem, request_id, text[:LOG_BODY_LIMIT],
        )
        return _synthetic_envelope(operation, request_id, "INTERNAL_ERROR")
    body.setdefault("warnings", [])
    return body


def _public_error(error):
    """Only the error fields the API exposes (details is left out, e.g. S3 keys)."""
    if not error:
        return None
    return {k: error[k] for k in ("code", "category", "retryable", "message")}


def _public_warnings(warnings):
    """Warnings {code, message, lead?} without duplicates; malformed items are dropped
    (contract 2.0 per-method warnings do not go through envelope validation)."""
    out, seen = [], set()
    for w in warnings if isinstance(warnings, list) else []:
        if not (isinstance(w, dict) and _is_str(w.get("code")) and isinstance(w.get("message"), str)):
            continue
        item = {"code": w["code"], "message": w["message"]}
        if _is_str(w.get("lead")):
            item["lead"] = w["lead"]
        key = (item["code"], item.get("lead"))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# --------------------------------------------------------------------------- estado


def _finish(attempt, phase, status, **fields):
    """Stores the phase outcome only if the attempt is still in that phase's processing state (CAS).

    A late/duplicate result (e.g. after a TASK_TIMEOUT) does not overwrite another state."""
    done = transition(attempt, PHASES[phase][0], status, **fields)
    if not done:
        logger.warning("Outcome %s of phase %s dropped: attempt %s is already in %s",
                       status, phase, attempt.id, attempt.status)
    return done


def _mirror_exam(attempt_id, exam_id, status, now):
    latest = (
        ExamAttempt.objects.filter(exam_id=exam_id)
        .order_by("-created_at").values_list("id", flat=True).first()
    )
    if latest == attempt_id:
        Exam.objects.filter(pk=exam_id).update(status=status, updated_at=now)


def transition(attempt, from_statuses, to_status, updated_before=None, **fields):
    """Compare-and-swap: changes the status only if the attempt is still in `from_statuses`.

    Two simultaneous taps (or two queue events) do not both get through. Mirrors to the exam if the
    attempt is the latest. Returns True if this call made the transition; updates `attempt`.
    """
    if isinstance(from_statuses, str):
        from_statuses = (from_statuses,)
    now = timezone.now()
    with transaction.atomic():
        qs = ExamAttempt.objects.filter(pk=attempt.pk, status__in=from_statuses)
        if updated_before is not None:
            qs = qs.filter(updated_at__lte=updated_before)
        updated = qs.update(status=to_status, updated_at=now, **fields)
        if updated:
            _mirror_exam(attempt.pk, attempt.exam_id, to_status, now)
    attempt.refresh_from_db()
    return bool(updated)


# Phase -> (processing state, error state, JSON field of the phase block).
PHASES = {
    "read": (ExamStatus.READING, ExamStatus.READING_ERROR, "reading"),
    "interpret": (ExamStatus.INTERPRETING, ExamStatus.INTERPRETATION_ERROR, "interpretation"),
}


def fail_phase(attempt, phase, code, details=None, updated_before=None):
    """Marks the processing phase as a local error (`INTERNAL_ERROR`, `TASK_TIMEOUT`...), via CAS.

    For interpretation the reading is kept (the retry reuses the signal)."""
    processing, error_status, field = PHASES[phase]
    synthetic = _synthetic_envelope(phase, str(attempt.id), code, details)
    if phase == "read":
        block = _empty_reading(synthetic)
        block["error"] = synthetic["error"]
    else:
        block = _interpretation_record(synthetic)
    return transition(attempt, processing, error_status, updated_before=updated_before, **{field: block})


def recover_stale(attempt):
    """An attempt stuck in a processing state beyond STALE_PROCESSING_SECONDS becomes an error state with
    `TASK_TIMEOUT` (retryable), e.g. the worker restarted mid-call. Returns the attempt."""
    for phase, (processing, _, _) in PHASES.items():
        if attempt.status != processing:
            continue
        limit = timezone.now() - timedelta(seconds=settings.STALE_PROCESSING_SECONDS)
        if attempt.updated_at <= limit and fail_phase(attempt, phase, "TASK_TIMEOUT", updated_before=limit):
            logger.warning("Attempt %s stuck in %s; marked as TASK_TIMEOUT", attempt.id, processing)
    return attempt


def _artifact(ref, request_id):
    """Validates a contract §3 artifact {bucket, key}: results bucket and attempt prefix."""
    if not isinstance(ref, dict):
        return None
    bucket, key = ref.get("bucket"), ref.get("key")
    if bucket != settings.AWS_RESULTS_BUCKET:
        return None
    if not _is_str(key) or not key.startswith(f"reading/{request_id}/") or ".." in key:
        return None
    return {"bucket": bucket, "key": key}


def _region_px(value):
    if (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        return value
    return None


def _empty_reading(envelope):
    return {
        "status": envelope["status"],
        "warnings": _public_warnings(envelope.get("warnings")),
        "error": None,
        "meta": envelope.get("meta") or {},
        "grid": None,
        "leads": [],
        "artifacts": {"overlay": None, "signal": None, "refusal_preview": None},
        "refusal_region_px": None,
    }


def _reading_error(envelope, code, request_id, reason):
    logger.error("Invalid reading %s: %s", request_id, reason)
    synthetic = _synthetic_envelope("read", request_id, code)
    reading = _empty_reading(synthetic)
    reading["meta"] = envelope.get("meta") or synthetic["meta"]
    reading["error"] = synthetic["error"]
    return reading


# --------------------------------------------------------------------------- reading


def build_reading_payload(attempt):
    payload = {
        "contract_version": CONTRACT_VERSION,
        "request_id": str(attempt.id),
        "image": {"bucket": settings.AWS_IMAGES_BUCKET, "key": attempt.image_s3_key},
        "layout": attempt.layout,
    }
    if attempt.calibration:
        payload["calibration"] = attempt.calibration
    mock = (attempt.test_scenario or {}).get("read")
    if mock:
        payload["mock"] = mock
    return payload


def _methods_summary(methods, request_id):
    """Summary of the six methods; `image` is the preview (`rejection_preview` when not OK) shown per method."""
    if not isinstance(methods, list):
        return []
    summary = []
    for m in methods:
        if not isinstance(m, dict):
            continue
        files = m.get("files") if isinstance(m.get("files"), dict) else {}
        item = {k: m.get(k) for k in ("method", "version", "status", "reason", "selector_score")}
        item["image"] = _artifact(files.get("image"), request_id)
        summary.append(item)
    return summary


def _choose_method(methods, request_id):
    """The `OK` method with the highest `selector_score` and valid `files.leads` and `files.image` (contract §3).

    Returns (method, leads.json ref, image ref) or None."""
    best = None
    for method in methods:
        if not isinstance(method, dict) or method.get("status") != METHOD_OK:
            continue
        score = method.get("selector_score")
        files = method.get("files") if isinstance(method.get("files"), dict) else {}
        leads = _artifact(files.get("leads"), request_id)
        image = _artifact(files.get("image"), request_id)
        if not _is_number(score) or leads is None or image is None:
            continue
        if best is None or score > best[0]["selector_score"]:
            best = (method, leads, image)
    return best


def _usable_methods(methods, request_id):
    """Every `OK` method with a score and valid `files.leads`/`files.image`, best score first."""
    usable = []
    for method in methods if isinstance(methods, list) else []:
        if not isinstance(method, dict) or method.get("status") != METHOD_OK:
            continue
        files = method.get("files") if isinstance(method.get("files"), dict) else {}
        leads = _artifact(files.get("leads"), request_id)
        image = _artifact(files.get("image"), request_id)
        if _is_number(method.get("selector_score")) and leads and image:
            usable.append({"id": method.get("method"), "score": method["selector_score"],
                           "overlay": image, "signal": leads})
    return sorted(usable, key=lambda variant: variant["score"], reverse=True)


def reading_variants(reading):
    """The reading's variants (one per `OK` method), best score first.

    Readings stored before variants existed only keep the method summary; the artifacts of every
    method share the chosen overlay's prefix (`reading/<request>/<run>/<method>/`), so they are rebuilt from it."""
    if not reading:
        return []
    if isinstance(reading.get("variants"), list):
        return reading["variants"]
    overlay = (reading.get("artifacts") or {}).get("overlay")
    if not overlay or reading.get("status") not in SUCCESS_STATUSES:
        return []
    parts = overlay["key"].split("/")
    if len(parts) < 5:
        return []
    prefix = "/".join(parts[:-2]) + "/"
    variants = []
    for method in reading.get("methods") or []:
        name = method.get("method") if isinstance(method, dict) else None
        if method.get("status") != METHOD_OK or not _is_str(name) or not _is_number(method.get("selector_score")):
            continue
        variants.append({
            "id": name,
            "score": method["selector_score"],
            "overlay": {"bucket": overlay["bucket"], "key": f"{prefix}{name}/preview.png"},
            "signal": {"bucket": overlay["bucket"], "key": f"{prefix}{name}/leads.json"},
        })
    return sorted(variants, key=lambda variant: variant["score"], reverse=True)


def choose_variant(attempt, variant_id):
    """The user picked another variant on the check screen: it becomes the reading's overlay and signal.

    Returns False if the id is not one of the reading's variants or its signal cannot be read."""
    reading = attempt.reading or {}
    variant = next((v for v in reading_variants(reading) if v["id"] == variant_id), None)
    if variant is None:
        return False
    if (reading.get("method") or {}).get("name") == variant_id:
        return True
    try:
        leads_doc = _read_json_from_s3(variant["signal"]["bucket"], variant["signal"]["key"])
        leads = leads_doc.get("leads") if isinstance(leads_doc, dict) else None
        if not isinstance(leads, list) or not all(isinstance(lead, dict) for lead in leads):
            raise ValueError("leads.json has no lead list")
    except Exception:
        logger.exception("Failed to read leads.json of variant %s (attempt %s)", variant_id, attempt.id)
        return False
    previous = reading.get("method") or {}
    reading["variants"] = reading_variants(reading)
    reading["leads"] = [{k: lead.get(k) for k in ("name", "kind", "quality", "coverage")} for lead in leads]
    reading["artifacts"] = {**(reading.get("artifacts") or {}), "overlay": variant["overlay"], "signal": variant["signal"]}
    reading["method"] = {
        "name": variant_id,
        "version": next((m.get("version") for m in reading.get("methods") or []
                         if isinstance(m, dict) and m.get("method") == variant_id), None),
        "selector_score": variant["score"],
        "sampling_rate_hz": leads_doc.get("sampling_rate_hz", previous.get("sampling_rate_hz")),
        "chosen_by_user": True,
    }
    attempt.reading = reading
    attempt.save(update_fields=["reading"])
    return True


def _refusal_preview(details, request_id):
    """Refusal preview: `details.preview` (old format) or the refusal image of the first method."""
    preview = _artifact(details.get("preview"), request_id)
    if preview:
        return preview
    for method in details.get("methods") if isinstance(details.get("methods"), list) else []:
        files = method.get("files") if isinstance(method, dict) and isinstance(method.get("files"), dict) else {}
        preview = _artifact(files.get("image"), request_id)
        if preview:
            return preview
    return None


def _read_success(envelope, reading, request_id):
    """Fills `reading` from an OK/OK_WITH_WARNINGS reading; returns the synthetic error block
    if the response is unusable."""
    methods = envelope["result"].get("methods")
    if not isinstance(methods, list):
        return _reading_error(envelope, "INTERNAL_ERROR", request_id, "result.methods ausente")
    chosen = _choose_method(methods, request_id)
    if chosen is None:
        return _reading_error(envelope, "INTERNAL_ERROR", request_id,
                              f"no usable OK method: {_methods_summary(methods, request_id)!r}"[:LOG_BODY_LIMIT])
    method, leads_ref, image_ref = chosen
    try:
        leads_doc = _read_json_from_s3(leads_ref["bucket"], leads_ref["key"])
        leads = leads_doc.get("leads") if isinstance(leads_doc, dict) else None
        if not isinstance(leads, list) or not all(isinstance(lead, dict) for lead in leads):
            raise ValueError("leads.json has no lead list")
    except Exception:
        logger.exception("Failed to read leads.json %s of reading %s", leads_ref, request_id)
        return _reading_error(envelope, "STORAGE_ERROR", request_id, "unreadable leads.json")

    warnings = _public_warnings((envelope.get("warnings") or []) + (method.get("warnings") or []))
    if envelope["status"] == STATUS_OK_WITH_WARNINGS and not warnings:
        warnings = [dict(METHODS_PARTIAL_WARNING)]
    reading["warnings"] = warnings
    # Per-lead summary WITHOUT `samples`: the signal stays only in S3 (artifacts.signal).
    reading["leads"] = [{k: lead.get(k) for k in ("name", "kind", "quality", "coverage")} for lead in leads]
    reading["artifacts"]["overlay"] = image_ref
    reading["artifacts"]["signal"] = leads_ref
    reading["method"] = {
        "name": method.get("method"),
        "version": method.get("version"),
        "selector_score": method["selector_score"],
        "sampling_rate_hz": method.get("sampling_rate_hz"),
    }
    reading["methods"] = _methods_summary(methods, request_id)
    reading["variants"] = _usable_methods(methods, request_id)
    return reading


def run_reading(attempt):
    """Reading phase (contract 2.0). Stores `attempt.reading` and moves to
    awaiting_validation / reading_refused / reading_error. Devolve o status final."""
    request_id = str(attempt.id)
    envelope = invoke_contract(settings.AWS_LAMBDA_ECG_READING, build_reading_payload(attempt), "read")
    status = envelope["status"]
    reading = _empty_reading(envelope)

    if status in SUCCESS_STATUSES:
        reading = _read_success(envelope, reading, request_id)
        if reading["error"] is None:
            _finish(attempt, "read", ExamStatus.AWAITING_VALIDATION, reading=reading)
            return attempt.status

    elif status == STATUS_REFUSED:
        error = envelope["error"]
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        reading["error"] = _public_error(error)
        # The preview is optional; outside the attempt's bucket/prefix it is dropped.
        reading["artifacts"]["refusal_preview"] = _refusal_preview(details, request_id)
        reading["refusal_region_px"] = _region_px(details.get("region_px"))
        reading["methods"] = _methods_summary(details.get("methods"), request_id)
        _finish(attempt, "read", ExamStatus.READING_REFUSED, reading=reading)
        return attempt.status

    else:
        error = envelope["error"]
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        reading["error"] = _public_error(error)
        reading["methods"] = _methods_summary(details.get("methods"), request_id)

    _finish(attempt, "read", ExamStatus.READING_ERROR, reading=reading)
    return attempt.status


# --------------------------------------------------------------------------- interpretation


def _interpretation_record(envelope):
    return {
        "status": envelope["status"],
        "warnings": _public_warnings(envelope.get("warnings")),
        "error": _public_error(envelope.get("error")),
        "meta": envelope.get("meta") or {},
    }


def run_interpretation(attempt):
    """Interpretation phase (contract §3). Only from an OK/OK_WITH_WARNINGS reading.

    Stores `attempt.interpretation` (+ `Report` on success) and moves to completed /
    interpretation_error. Devolve o status final.
    """
    reading = attempt.reading or {}
    signal_ref = (reading.get("artifacts") or {}).get("signal")
    if reading.get("status") not in SUCCESS_STATUSES or not signal_ref:
        raise ValueError(f"Attempt {attempt.id} has no successful reading to interpret.")

    request_id = str(attempt.id)
    sex = attempt.exam.patient.sex
    if sex not in ("F", "M"):
        envelope = _synthetic_envelope("interpret", request_id, "SEX_MISSING")
    else:
        try:
            leads_doc = _read_json_from_s3(signal_ref["bucket"], signal_ref["key"])
            if not isinstance(leads_doc, dict):
                raise ValueError("leads.json is not an object")
            # The contract 2.0 leads.json has no sample rate: it comes from the chosen method (contract §4).
            signal = {"sampling_rate_hz": (reading.get("method") or {}).get("sampling_rate_hz"),
                      "units": "mV", **leads_doc}
        except Exception:
            logger.exception("Failed to download signal %s of attempt %s", signal_ref, request_id)
            envelope = _synthetic_envelope("interpret", request_id, "STORAGE_ERROR")
        else:
            payload = {
                "contract_version": CONTRACT_VERSION,
                "request_id": request_id,
                "patient": {"sex": sex},
                "signal": signal,
            }
            mock = (attempt.test_scenario or {}).get("interpret")
            if mock:
                payload["mock"] = mock
            envelope = invoke_contract(settings.AWS_LAMBDA_ECG_INTERPRETATION, payload, "interpret")

    if envelope["status"] in SUCCESS_STATUSES:
        result = envelope["result"]
        summary = result.get("summary")
        if not isinstance(summary, dict):
            logger.error("Interpretation %s has no summary: %s", request_id, str(result)[:LOG_BODY_LIMIT])
            envelope = _synthetic_envelope("interpret", request_id, "INTERNAL_ERROR")
        else:
            record = _interpretation_record(envelope)
            meta = record["meta"]
            classification = summary.get("classification")
            with transaction.atomic():
                if not _finish(attempt, "interpret", ExamStatus.COMPLETED, interpretation=record):
                    return attempt.status
                Report.objects.update_or_create(
                    attempt=attempt,
                    defaults={
                        "classification": classification if classification in Classification.values else None,
                        "summary": summary,
                        "details": result.get("details") if isinstance(result.get("details"), dict) else None,
                        "disclaimer": result.get("disclaimer") or "",
                        "warnings": record["warnings"],
                        "engine": str(meta.get("engine") or "")[:100],
                        "engine_version": str(meta.get("engine_version") or "")[:100],
                    },
                )
            return attempt.status

    _finish(attempt, "interpret", ExamStatus.INTERPRETATION_ERROR, interpretation=_interpretation_record(envelope))
    return attempt.status


# --------------------------------------------------------------------------- API views


def _presign_artifact(ref, expires_in):
    if not ref:
        return None
    return presign_get_url(ref["bucket"], ref["key"], expires_in)


def attempt_reading_view(attempt, expires_in=None):
    """`reading` block of the `Attempt` object (plan §0.1), or None if the reading did not run."""
    reading = attempt.reading
    if not reading:
        return None
    expires_in = expires_in or settings.PRESIGN_URL_EXPIRES
    artifacts = reading.get("artifacts") or {}
    chosen = (reading.get("method") or {}).get("name")
    return {
        "status": reading.get("status"),
        "warnings": reading.get("warnings") or [],
        "error": reading.get("error"),
        "overlay_url": _presign_artifact(artifacts.get("overlay"), expires_in),
        "refusal_preview_url": _presign_artifact(artifacts.get("refusal_preview"), expires_in),
        "refusal_region_px": reading.get("refusal_region_px"),
        "leads": reading.get("leads") or [],
        # Score is kept for ordering only; the app never shows it.
        "variants": [
            {"id": v["id"], "overlay_url": _presign_artifact(v["overlay"], expires_in), "score": v["score"]}
            for v in reading_variants(reading)
        ],
        "methods": [
            {
                "method": m.get("method"),
                "status": m.get("status"),
                "selector_score": m.get("selector_score"),
                "chosen": m.get("method") == chosen,
                "image_url": _presign_artifact(m.get("image"), expires_in),
            }
            for m in reading.get("methods") or []
        ],
    }


def attempt_interpretation_view(attempt):
    """`interpretation` block of the `Attempt` object (plan §0.1), or None if it did not run."""
    record = attempt.interpretation
    if not record:
        return None
    view = {
        "status": record.get("status"),
        "warnings": record.get("warnings") or [],
        "error": record.get("error"),
        "summary": None,
        "details": None,
        "disclaimer": None,
    }
    if record.get("status") in SUCCESS_STATUSES:
        try:
            report = attempt.report  # reuses the view's select_related/prefetch_related
        except Report.DoesNotExist:
            report = None
        if report is not None:
            view["summary"] = report.summary
            view["details"] = report.details
            view["disclaimer"] = report.disclaimer or None
    return view


def phase_error(attempt):
    """Error of the failed phase (`reading_error` -> reading, `interpretation_error` -> interpretation)."""
    if attempt.status == ExamStatus.READING_ERROR:
        return (attempt.reading or {}).get("error")
    if attempt.status == ExamStatus.INTERPRETATION_ERROR:
        return (attempt.interpretation or {}).get("error")
    return None


def attempt_flags(attempt):
    """`can_retry` / `needs_new_photo` from flow §3."""
    status = attempt.status
    if status in (ExamStatus.READING_ERROR, ExamStatus.INTERPRETATION_ERROR):
        error = phase_error(attempt) or {}
        # No recorded error (should not happen): allow a retry.
        retryable = error.get("retryable", True) is True
        return {"can_retry": retryable, "needs_new_photo": not retryable}
    if status in (ExamStatus.READING_REFUSED, ExamStatus.USER_REJECTED):
        return {"can_retry": False, "needs_new_photo": True}
    return {"can_retry": False, "needs_new_photo": False}
