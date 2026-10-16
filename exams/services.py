"""Integração Django com AWS S3 e as lambdas do contrato v1 (CONTRATO_LAMBDAS.md).

- `invoke_contract` sempre devolve um envelope do contrato (falha de infraestrutura vira
  um envelope sintético `ERROR`/`INTERNAL_ERROR` retryable, nunca exceção).
- `run_reading` / `run_interpretation` executam uma fase e gravam o desfecho na tentativa
  (estados de docs/fluxo-usuario-v1.md §3). São síncronas: quem decide se rodam inline ou
  no worker é `exams/tasks.py` (etapa B3).
- `attempt_reading_view` / `attempt_interpretation_view` / `attempt_flags` montam os blocos
  do objeto `Attempt` da API (plano §0.1); presigned URLs são geradas na hora, nunca gravadas.
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

CONTRACT_VERSION = "1.0"

STATUS_OK = "OK"
STATUS_OK_WITH_WARNINGS = "OK_WITH_WARNINGS"
STATUS_REFUSED = "REFUSED"
STATUS_ERROR = "ERROR"
SUCCESS_STATUSES = (STATUS_OK, STATUS_OK_WITH_WARNINGS)
READ_STATUSES = (STATUS_OK, STATUS_OK_WITH_WARNINGS, STATUS_REFUSED, STATUS_ERROR)
INTERPRET_STATUSES = (STATUS_OK, STATUS_OK_WITH_WARNINGS, STATUS_ERROR)

# Erros que o próprio backend gera (sem resposta válida da lambda). Mesmos códigos do catálogo.
LOCAL_ERRORS = {
    "INTERNAL_ERROR": ("internal", True, "Não foi possível concluir o processamento agora. Tente de novo em instantes."),
    "STORAGE_ERROR": ("internal", True, "Falha ao ler os artefatos da leitura. Tente de novo em instantes."),
    "SEX_MISSING": ("validation", False, "Informe o sexo do paciente para interpretar o ECG."),
    "TASK_TIMEOUT": ("internal", True, "O processamento demorou mais que o esperado. Tente de novo."),
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
    # Espera a lambda terminar (até AWS_LAMBDA_READ_TIMEOUT) e nunca reinvoca sozinho:
    # quem decide repetir é o usuário (retry) ou a fila.
    return _session().client(
        "lambda",
        config=Config(
            read_timeout=settings.AWS_LAMBDA_READ_TIMEOUT,
            connect_timeout=10,
            retries={"max_attempts": 0},
        ),
    )


def _s3_client():
    """Client S3 forçado a SigV4 para presigned URLs (SigV2 deprecated by AWS)."""
    return _session().client("s3", config=Config(signature_version="s3v4"))


def presign_put_url(bucket, key, expires_in, content_type=None):
    """Presigned PUT direto do frontend pro S3 (upload nunca passa no Django).

    Se content_type for informado, ele é ASSINADO na URL: o cliente DEVE enviar
    o mesmo valor no header Content-Type, senão a assinatura falha (403).
    """
    params = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return _s3_client().generate_presigned_url(
        "put_object", Params=params, ExpiresIn=expires_in
    )


def presign_get_url(bucket, key, expires_in):
    """Presigned GET sob demanda — URLs nunca são persistidas no banco."""
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def original_image_url(attempt, expires_in):
    if not attempt.imagem_s3_key:
        return None
    return presign_get_url(settings.AWS_IMAGES_BUCKET, attempt.imagem_s3_key, expires_in)


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


def _envelope_problem(body, operation, request_id):
    """Devolve o motivo de o envelope ser inválido, ou None se ele seguir o contrato §1.1."""
    if not isinstance(body, dict):
        return "corpo não é objeto"
    allowed = READ_STATUSES if operation == "read" else INTERPRET_STATUSES
    status = body.get("status")
    if status not in allowed:
        return f"status inválido: {status!r}"
    if body.get("request_id") not in (None, request_id):
        return "request_id diferente do enviado"
    warnings = body.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(w, dict) and _is_str(w.get("code")) and isinstance(w.get("message"), str)
        for w in warnings
    ):
        return "warnings inválido"
    if status in SUCCESS_STATUSES:
        if not isinstance(body.get("result"), dict):
            return "result ausente em status de sucesso"
    else:
        error = body.get("error")
        if not (
            isinstance(error, dict)
            and _is_str(error.get("code"))
            and _is_str(error.get("category"))
            and isinstance(error.get("retryable"), bool)
            and isinstance(error.get("message"), str)
        ):
            return "error ausente ou incompleto"
    return None


def invoke_contract(function_name, payload, operation=None):
    """Invoca a lambda (RequestResponse) e devolve SEMPRE um envelope do contrato v1.

    FunctionError (timeout/OOM/exceção), erro do boto3/rede ou envelope fora do contrato viram
    um envelope sintético `ERROR` / `INTERNAL_ERROR` com `retryable: true`.
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
    except Exception:  # timeout de leitura, ClientError, conexão, credenciais
        logger.exception("Falha ao invocar a lambda %s (request_id=%s)", function_name, request_id)
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
            "Lambda %s devolveu envelope fora do contrato (%s, request_id=%s): %s",
            function_name, problem, request_id, text[:LOG_BODY_LIMIT],
        )
        return _synthetic_envelope(operation, request_id, "INTERNAL_ERROR")
    body.setdefault("warnings", [])
    return body


def _public_error(error):
    """Só os campos do erro que a API expõe (details fica de fora; ex.: chaves S3)."""
    if not error:
        return None
    return {k: error[k] for k in ("code", "category", "retryable", "message")}


def _public_warnings(warnings):
    out = []
    for w in warnings or []:
        item = {"code": w["code"], "message": w["message"]}
        if w.get("lead"):
            item["lead"] = w["lead"]
        out.append(item)
    return out


# --------------------------------------------------------------------------- estado


def _finish(attempt, phase, status, **fields):
    """Grava o desfecho da fase só se a tentativa ainda estiver no `*_processando` dela (CAS).

    Um resultado atrasado/duplicado (ex.: depois de um TASK_TIMEOUT) não sobrescreve outro estado."""
    done = transition(attempt, PHASES[phase][0], status, **fields)
    if not done:
        logger.warning("Desfecho %s da fase %s descartado: tentativa %s já está em %s",
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
    """Compare-and-swap: muda o status só se a tentativa ainda estiver em `from_statuses`.

    Dois toques simultâneos (ou dois eventos da fila) não passam os dois. Espelha no exame se a
    tentativa for a mais recente. Devolve True se esta chamada fez a transição; atualiza `attempt`.
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


# Fase → (estado em processamento, estado de erro, campo JSON do bloco da fase).
PHASES = {
    "read": (ExamStatus.LEITURA_PROCESSANDO, ExamStatus.ERRO_LEITURA, "reading"),
    "interpret": (ExamStatus.INTERPRETACAO_PROCESSANDO, ExamStatus.ERRO_INTERPRETACAO, "interpretation"),
}


def fail_phase(attempt, phase, code, details=None, updated_before=None):
    """Marca a fase em processamento como erro local (`INTERNAL_ERROR`, `TASK_TIMEOUT`…), via CAS.

    Na interpretação a leitura é preservada (o retry reaproveita o sinal)."""
    processing, error_status, field = PHASES[phase]
    synthetic = _synthetic_envelope(phase, str(attempt.id), code, details)
    if phase == "read":
        block = _empty_reading(synthetic)
        block["error"] = synthetic["error"]
    else:
        block = _interpretation_record(synthetic)
    return transition(attempt, processing, error_status, updated_before=updated_before, **{field: block})


def recover_stale(attempt):
    """Tentativa presa em `*_processando` além de STALE_PROCESSING_SECONDS vira `erro_*` com
    `TASK_TIMEOUT` (retryable). Ex.: o worker reiniciou no meio da chamada. Devolve a tentativa."""
    for phase, (processing, _, _) in PHASES.items():
        if attempt.status != processing:
            continue
        limit = timezone.now() - timedelta(seconds=settings.STALE_PROCESSING_SECONDS)
        if attempt.updated_at <= limit and fail_phase(attempt, phase, "TASK_TIMEOUT", updated_before=limit):
            logger.warning("Tentativa %s presa em %s; marcada como TASK_TIMEOUT", attempt.id, processing)
    return attempt


def _artifact(ref, request_id):
    """Valida um artefato {bucket, key} do contrato §2.3: bucket de resultados e prefixo da tentativa."""
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
    logger.error("Leitura %s inválida: %s", request_id, reason)
    synthetic = _synthetic_envelope("read", request_id, code)
    reading = _empty_reading(synthetic)
    reading["meta"] = envelope.get("meta") or synthetic["meta"]
    reading["error"] = synthetic["error"]
    return reading


# --------------------------------------------------------------------------- leitura


def build_reading_payload(attempt):
    payload = {
        "contract_version": CONTRACT_VERSION,
        "request_id": str(attempt.id),
        "image": {"bucket": settings.AWS_IMAGES_BUCKET, "key": attempt.imagem_s3_key},
        "layout": attempt.layout,
    }
    if attempt.calibracao:
        payload["calibration"] = attempt.calibracao
    mock = (attempt.test_scenario or {}).get("read")
    if mock:
        payload["mock"] = mock
    return payload


def run_reading(attempt):
    """Fase de leitura (contrato §2). Grava `attempt.reading` e move para
    aguardando_validacao / leitura_recusada / erro_leitura. Devolve o status final."""
    request_id = str(attempt.id)
    envelope = invoke_contract(settings.AWS_LAMBDA_ECG_READING, build_reading_payload(attempt), "read")
    status = envelope["status"]
    reading = _empty_reading(envelope)

    if status in SUCCESS_STATUSES:
        result = envelope["result"]
        artifacts = result.get("artifacts") or {}
        overlay = _artifact(artifacts.get("overlay"), request_id)
        signal = _artifact(artifacts.get("signal"), request_id)
        leads = result.get("leads")
        if overlay is None or signal is None:
            reading = _reading_error(envelope, "INTERNAL_ERROR", request_id,
                                     f"artefatos fora do bucket/prefixo: {artifacts!r}"[:LOG_BODY_LIMIT])
        elif not isinstance(leads, list) or not all(isinstance(lead, dict) for lead in leads):
            reading = _reading_error(envelope, "INTERNAL_ERROR", request_id, "leads ausente")
        else:
            reading["grid"] = result.get("grid")
            # Resumo por derivação SEM `samples`: o sinal fica só no S3 (artifacts.signal).
            reading["leads"] = [
                {k: lead.get(k) for k in ("name", "kind", "quality", "coverage")} for lead in leads
            ]
            reading["artifacts"]["overlay"] = overlay
            reading["artifacts"]["signal"] = signal
            _finish(attempt, "read", ExamStatus.AGUARDANDO_VALIDACAO, reading=reading)
            return attempt.status

    elif status == STATUS_REFUSED:
        error = envelope["error"]
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        reading["error"] = _public_error(error)
        # A prévia é opcional no contrato (§6.3); fora do bucket/prefixo ela é descartada.
        reading["artifacts"]["refusal_preview"] = _artifact(details.get("preview"), request_id)
        reading["refusal_region_px"] = _region_px(details.get("region_px"))
        _finish(attempt, "read", ExamStatus.LEITURA_RECUSADA, reading=reading)
        return attempt.status

    else:
        reading["error"] = _public_error(envelope["error"])

    _finish(attempt, "read", ExamStatus.ERRO_LEITURA, reading=reading)
    return attempt.status


# --------------------------------------------------------------------------- interpretação


def _interpretation_record(envelope):
    return {
        "status": envelope["status"],
        "warnings": _public_warnings(envelope.get("warnings")),
        "error": _public_error(envelope.get("error")),
        "meta": envelope.get("meta") or {},
    }


def run_interpretation(attempt):
    """Fase de interpretação (contrato §3). Só a partir de uma leitura OK/OK_WITH_WARNINGS.

    Grava `attempt.interpretation` (+ `Report` no sucesso) e move para concluido /
    erro_interpretacao. Devolve o status final.
    """
    reading = attempt.reading or {}
    signal_ref = (reading.get("artifacts") or {}).get("signal")
    if reading.get("status") not in SUCCESS_STATUSES or not signal_ref:
        raise ValueError(f"Tentativa {attempt.id} sem leitura bem-sucedida para interpretar.")

    request_id = str(attempt.id)
    sex = attempt.exam.patient.sex
    if sex not in ("F", "M"):
        envelope = _synthetic_envelope("interpret", request_id, "SEX_MISSING")
    else:
        try:
            signal = _read_json_from_s3(signal_ref["bucket"], signal_ref["key"])
            if not isinstance(signal, dict):
                raise ValueError("signal.json não é objeto")
        except Exception:
            logger.exception("Falha ao baixar o sinal %s da tentativa %s", signal_ref, request_id)
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
            logger.error("Interpretação %s sem summary: %s", request_id, str(result)[:LOG_BODY_LIMIT])
            envelope = _synthetic_envelope("interpret", request_id, "INTERNAL_ERROR")
        else:
            record = _interpretation_record(envelope)
            meta = record["meta"]
            classification = summary.get("classification")
            with transaction.atomic():
                if not _finish(attempt, "interpret", ExamStatus.CONCLUIDO, interpretation=record):
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

    _finish(attempt, "interpret", ExamStatus.ERRO_INTERPRETACAO, interpretation=_interpretation_record(envelope))
    return attempt.status


# --------------------------------------------------------------------------- leitura para a API


def _presign_artifact(ref, expires_in):
    if not ref:
        return None
    return presign_get_url(ref["bucket"], ref["key"], expires_in)


def attempt_reading_view(attempt, expires_in=None):
    """Bloco `reading` do objeto `Attempt` (plano §0.1), ou None se a leitura não rodou."""
    reading = attempt.reading
    if not reading:
        return None
    expires_in = expires_in or settings.PRESIGN_URL_EXPIRES
    artifacts = reading.get("artifacts") or {}
    return {
        "status": reading.get("status"),
        "warnings": reading.get("warnings") or [],
        "error": reading.get("error"),
        "overlay_url": _presign_artifact(artifacts.get("overlay"), expires_in),
        "refusal_preview_url": _presign_artifact(artifacts.get("refusal_preview"), expires_in),
        "refusal_region_px": reading.get("refusal_region_px"),
        "leads": reading.get("leads") or [],
    }


def attempt_interpretation_view(attempt):
    """Bloco `interpretation` do objeto `Attempt` (plano §0.1), ou None se não rodou."""
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
            report = attempt.report  # aproveita select_related/prefetch_related da view
        except Report.DoesNotExist:
            report = None
        if report is not None:
            view["summary"] = report.summary
            view["details"] = report.details
            view["disclaimer"] = report.disclaimer or None
    return view


def phase_error(attempt):
    """Erro da fase que falhou (`erro_leitura` → reading, `erro_interpretacao` → interpretation)."""
    if attempt.status == ExamStatus.ERRO_LEITURA:
        return (attempt.reading or {}).get("error")
    if attempt.status == ExamStatus.ERRO_INTERPRETACAO:
        return (attempt.interpretation or {}).get("error")
    return None


def attempt_flags(attempt):
    """`can_retry` / `needs_new_photo` do fluxo §3."""
    status = attempt.status
    if status in (ExamStatus.ERRO_LEITURA, ExamStatus.ERRO_INTERPRETACAO):
        error = phase_error(attempt) or {}
        # Sem erro registrado (não deveria acontecer) vale tentar de novo.
        retryable = error.get("retryable", True) is True
        return {"can_retry": retryable, "needs_new_photo": not retryable}
    if status in (ExamStatus.LEITURA_RECUSADA, ExamStatus.REJEITADO_USUARIO):
        return {"can_retry": False, "needs_new_photo": True}
    return {"can_retry": False, "needs_new_photo": False}
