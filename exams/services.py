"""Integração Django com AWS S3 e uma única Lambda ECG Analyser.

`run_phase_a` invoca `operation=analyze`, `run_refine` invoca
`operation=refine` na mesma função e `run_phase_b` confirma apenas
a extração dos traçados, sem diagnóstico clínico automatizado.
"""

import json
import logging

import boto3
from botocore.config import Config

from django.conf import settings

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Falha ao invocar o pipeline (lambda/S3). Virado 502 na view."""


def _session():
    if settings.AWS_PROFILE_NAME:
        return boto3.Session(
            profile_name=settings.AWS_PROFILE_NAME,
            region_name=settings.AWS_REGION,
        )
    return boto3.Session(region_name=settings.AWS_REGION)


def _lambda_client():
    # O cliente precisa tolerar o tempo de resposta da Lambda ECG Analyser.
    return _session().client(
        "lambda",
        config=Config(
            read_timeout=settings.AWS_LAMBDA_READ_TIMEOUT,
            connect_timeout=10,
            retries={"max_attempts": 2},
        ),
    )


def _s3_client():
    """Client S3 forçado a SigV4 para presigned URLs (SigV2 deprecated by AWS)."""
    return _session().client("s3", config=Config(signature_version="s3v4"))


def invoke_lambda(function_name, payload):
    """Invoca uma lambda sincronamente (RequestResponse) e devolve o dict.

    Levanta UpstreamError em erro de invocação ou FunctionError da lambda.
    """
    try:
        response = _lambda_client().invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        body = json.loads(response["Payload"].read())
    except Exception as exc:  # TimeoutError, ClientError, conexão, serialização
        logger.exception("Falha ao invocar lambda %s", function_name)
        raise UpstreamError(f"{function_name}: {exc}") from exc

    if response.get("FunctionError"):
        detail = body.get("errorMessage", str(body)) if isinstance(body, dict) else str(body)
        logger.error("Lambda %s retornou erro: %s", function_name, detail)
        raise UpstreamError(f"{function_name}: {detail}")

    return body


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


def overlay_urls(attempt, expires_in):
    return {
        "overlay_trace_url": (
            presign_get_url(settings.AWS_RESULTS_BUCKET, attempt.overlay_trace_s3_key, expires_in)
            if attempt.overlay_trace_s3_key
            else None
        ),
        "overlay_signal_url": (
            presign_get_url(settings.AWS_RESULTS_BUCKET, attempt.overlay_signal_s3_key, expires_in)
            if attempt.overlay_signal_s3_key
            else None
        ),
    }


def run_phase_a(attempt, exam):
    """Invoke only ecg-analyser. No legacy diagnosis Lambda."""
    calibration = getattr(attempt, "calibracao", None)
    if (not isinstance(calibration, dict) or
        not all(calibration.get(k) is not None for k in ("speed_mm_s", "gain_mm_mv"))):
        raise UpstreamError("Calibração explícita ausente; informe velocidade e ganho no aplicativo")
    output = invoke_lambda(settings.AWS_LAMBDA_ECG_ANALYSER, {
        "operation": "analyze",
        "calibration": {"speed_mm_s": calibration["speed_mm_s"],
                        "gain_mm_mv": calibration["gain_mm_mv"]},
        "exame_id": str(exam.id),
        "attempt_id": str(attempt.id),
        "imagem_s3_key": attempt.imagem_s3_key,
        "images_bucket": settings.AWS_IMAGES_BUCKET,
        "results_bucket": settings.AWS_RESULTS_BUCKET,
    })
    if not isinstance(output, dict) or not isinstance(output.get("status"), str):
        raise UpstreamError("Resposta da Lambda sem status")
    status = output["status"]
    if status not in ("CONSISTENCY_PASS", "REVIEW_REQUIRED", "REJECTED_LOW_TRACE_GRID_CONTRAST"):
        raise UpstreamError("Status inesperado da Lambda: " + status)
    keys = output.get("artifacts", {})
    if not isinstance(keys, dict):
        raise UpstreamError("artifacts inválidos")
    prefix = f"ecg-analyser/{attempt.id}/initial/"
    filename_by_field = {"result": "result.json", "signals": "signals.npz",
                         "trace_masks": "trace_masks.npz", "working_image": "working_image.png",
                         "visao_geral": "visao_geral.png", "rejection_preview": "rejection_preview.png"}
    if any(field not in filename_by_field or not isinstance(key, str) or
           key != prefix + filename_by_field[field] for field, key in keys.items()):
        raise UpstreamError("A Lambda retornou artefatos fora da tentativa ou inválidos")
    needed = ("result",) if status.startswith("REJECTED_") else ("result", "signals", "trace_masks", "working_image", "visao_geral")
    if any(not isinstance(keys.get(k), str) or not keys.get(k) for k in needed):
        raise UpstreamError("Lambda não retornou todos os artefatos necessários")
    attempt.overlay_trace_s3_key = keys.get("visao_geral")
    attempt.overlay_signal_s3_key = None
    attempt.mascara_s3_key = keys.get("trace_masks")
    attempt.image_type = "ecg_analyser"
    # Metadata and S3 references, never fabricate the diagnostic output.
    attempt.sinais = {"analyser_status": status, "warnings": output.get("warnings", []),
                      "result_s3_key": keys.get("result"),
                      "signals_s3_key": keys.get("signals"),
                      "working_image_s3_key": keys.get("working_image"),
                      "leads": output.get("leads", [])}
    attempt.lead_labels = [lead.get("name") for lead in output.get("leads", []) if isinstance(lead, dict)]
    attempt.fidelidade = ("ruim" if status.startswith("REJECTED_") else
                          "parcial" if status == "REVIEW_REQUIRED" else "bom")
    attempt.fidelidade_por_derivacao = None
    # Preserve the explicit paper speed / gain selected by the user. A prior
    # implementation accidentally overwrote these settings with the grid fit.
    attempt.sinais["grid"] = output.get("grid")
    attempt.save()
    urls = overlay_urls(attempt, settings.PRESIGN_URL_EXPIRES)
    return {**urls, "imagem_original_url": original_image_url(attempt, settings.PRESIGN_URL_EXPIRES),
            "fidelidade": attempt.fidelidade, "image_type": attempt.image_type,
            "analyser_status": status, "warnings": output.get("warnings", []),
            "leads": attempt.lead_labels,
            "result_url": presign_get_url(settings.AWS_RESULTS_BUCKET, keys["result"], settings.PRESIGN_URL_EXPIRES) if keys.get("result") else None,
            "working_image_url": presign_get_url(settings.AWS_RESULTS_BUCKET, keys["working_image"], settings.PRESIGN_URL_EXPIRES) if keys.get("working_image") else None,
            "signals_url": presign_get_url(settings.AWS_RESULTS_BUCKET, keys["signals"], settings.PRESIGN_URL_EXPIRES) if keys.get("signals") else None}



def run_refine(attempt, exam, lead, strokes):
    """One Lambda call, immutable artifacts; do not overwrite previous run on error."""
    old=attempt.sinais or {}
    keys={"result":old.get("result_s3_key"),"signals":old.get("signals_s3_key"),
          "working_image":old.get("working_image_s3_key"),
          "trace_masks":attempt.mascara_s3_key,"visao_geral":attempt.overlay_trace_s3_key}
    if not all(keys.values()):
        raise UpstreamError("Artefatos para revisão indisponíveis; reexecute a análise")
    result=invoke_lambda(settings.AWS_LAMBDA_ECG_ANALYSER,{
        "operation":"refine", "attempt_id":str(attempt.id), "exame_id":str(exam.id),
        "results_bucket":settings.AWS_RESULTS_BUCKET,"lead":lead,
        "strokes":strokes,"parent_artifacts":keys})
    if not isinstance(result,dict) or result.get('status')!='MANUAL_REVIEW_REQUIRED':
        raise UpstreamError('Resposta inesperada do refinamento')
    new=result.get('artifacts')
    if not isinstance(new,dict) or not all(new.get(k) for k in ('result','signals','trace_masks','working_image','correction_preview')):
        raise UpstreamError('Refinamento não produziu todos os artefatos')
    prefix=f'ecg-analyser/{attempt.id}/'
    expected_filenames={'result':'result.json','signals':'signals.npz',
        'trace_masks':'trace_masks.npz','working_image':'working_image.png',
        'correction_preview':'correction_preview.png', 'visao_geral':None}
    for field, artifact in new.items():
        if field not in expected_filenames or not isinstance(artifact,str) or not artifact.startswith(prefix):
            raise UpstreamError('Refinamento retornou artefato fora da tentativa')
        expected_filename=expected_filenames[field]
        if expected_filename and not artifact.endswith('/'+expected_filename):
            raise UpstreamError('Refinamento retornou nome de artefato inesperado')
    history=old.get('correction_history',[])
    if not isinstance(history, list):
        raise UpstreamError('Histórico de revisões inválido')
    previous={"result":keys['result'],"signals":keys['signals'],"trace_masks":keys['trace_masks'],
              "working_image":keys['working_image'],"manual_correction":old.get('manual_correction')}
    next_data={**old,"analyser_status":result['status'],"warnings":result.get('warnings',[]),
               "result_s3_key":new['result'],"signals_s3_key":new['signals'],
               "working_image_s3_key":new['working_image'],"correction_preview_s3_key":new['correction_preview'],
               "manual_correction":result.get('manual_correction'),"correction_history":[*history,previous]}
    attempt.sinais=next_data
    attempt.mascara_s3_key=new['trace_masks']
    attempt.overlay_trace_s3_key=new['correction_preview']
    attempt.fidelidade='parcial'
    attempt.save(update_fields=['sinais','mascara_s3_key','overlay_trace_s3_key','fidelidade','updated_at'])
    return {"status":result['status'],"detail":"Correção registrada. Amostras modificadas requerem revisão manual.",
            "correction_preview_url":presign_get_url(settings.AWS_RESULTS_BUCKET,new['correction_preview'],settings.PRESIGN_URL_EXPIRES),
            "manual_correction":result.get('manual_correction')}


def run_phase_b(attempt, exam):
    """Approve extracted trace only. No algorithmic medical diagnosis."""
    from .models import Report
    if not attempt.sinais or attempt.sinais.get("analyser_status", "").startswith("REJECTED_"):
        raise UpstreamError("Extração rejeitada ou ausente: aprovação bloqueada")
    report, _ = Report.objects.update_or_create(
        attempt=attempt,
        defaults={"ritmo": None, "eixo_graus": None, "sokolow_mm": None,
                  "derivacoes": None, "indicadores": None, "achados": None,
                  "conclusao": None, "confiabilidade": attempt.fidelidade,
                  "versao_codigo": "ecg-analyser-extraction-only"},
    )
    return report, {"imagem_original_url": original_image_url(attempt, settings.PRESIGN_URL_EXPIRES),
                    **overlay_urls(attempt, settings.PRESIGN_URL_EXPIRES),
                    "clinical_diagnosis_available": False}
