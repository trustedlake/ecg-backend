"""Integração Django com AWS S3 e as Lambdas ECG.

`run_phase_a` invoca a Lambda de leitura v1 (`operation=read`, contrato 2.0), `run_refine` invoca
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


def _read_json(bucket, key):
    body = _session().client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def run_phase_a(attempt, exam):
    """Invoke the v1 reading Lambda (contract 2.0) and keep the best method.

    The Lambda returns 6 methods; the one with the highest selector_score
    among the `OK` ones is stored. No diagnosis is produced here.
    """
    calibration = getattr(attempt, "calibracao", None)
    if (not isinstance(calibration, dict) or
        not all(calibration.get(k) is not None for k in ("speed_mm_s", "gain_mm_mv"))):
        raise UpstreamError("Calibração explícita ausente; informe velocidade e ganho no aplicativo")
    output = invoke_lambda(settings.AWS_LAMBDA_ECG_READING, {
        "contract_version": "2.0",
        "operation": "read",
        "request_id": str(attempt.id),
        "layout": exam.ecg_layout,
        "calibration": {"speed_mm_s": calibration["speed_mm_s"],
                        "gain_mm_mv": calibration["gain_mm_mv"]},
        "image": {"bucket": settings.AWS_IMAGES_BUCKET, "key": attempt.imagem_s3_key},
    })
    if not isinstance(output, dict) or output.get("operation") != "read" or not isinstance(output.get("status"), str):
        raise UpstreamError("Resposta da Lambda de leitura inválida")
    envelope = output["status"]
    warnings = output.get("warnings") or []
    attempt.image_type = "ecg_analyser"
    attempt.overlay_signal_s3_key = None
    attempt.fidelidade_por_derivacao = None

    if envelope in ("OK", "OK_WITH_WARNINGS"):
        methods = (output.get("result") or {}).get("methods")
        if not isinstance(methods, list):
            raise UpstreamError("Resposta da Lambda sem methods")
        ok = [m for m in methods if isinstance(m, dict) and m.get("status") == "OK"
              and isinstance(m.get("selector_score"), (int, float))
              and isinstance((m.get("files") or {}).get("leads"), dict)]
        if not ok:
            raise UpstreamError("Nenhum método OK com selector_score")
        best = max(ok, key=lambda m: m["selector_score"])
        files = best["files"]
        leads_ref = files["leads"]
        leads = _read_json(leads_ref["bucket"], leads_ref["key"])
        lead_list = leads.get("leads", []) if isinstance(leads, dict) else []
        attempt.overlay_trace_s3_key = (files.get("image") or {}).get("key")
        attempt.mascara_s3_key = None
        attempt.lead_labels = [l.get("name") for l in lead_list if isinstance(l, dict)]
        attempt.fidelidade = "bom" if envelope == "OK" else "parcial"
        attempt.sinais = {
            "analyser_status": envelope, "warnings": warnings,
            "contract_version": output.get("contract_version"),
            "chosen_method": best["method"], "chosen_method_version": best.get("version"),
            "selector_score": best["selector_score"], "sampling_rate_hz": best.get("sampling_rate_hz"),
            "methods": [{k: m.get(k) for k in ("method", "version", "status", "reason", "selector_score")}
                        for m in methods if isinstance(m, dict)],
            "result_s3_key": (files.get("json") or {}).get("key"),
            "leads_s3_key": leads_ref["key"],
            "leads": lead_list,
        }
    elif envelope == "REFUSED":
        err = output.get("error") or {}
        attempt.overlay_trace_s3_key = None
        attempt.mascara_s3_key = None
        attempt.lead_labels = []
        attempt.fidelidade = "ruim"
        attempt.sinais = {"analyser_status": "REJECTED_" + str(err.get("code") or "UNREADABLE"),
                          "warnings": warnings, "error": err,
                          "result_s3_key": None, "leads": []}
    else:
        err = output.get("error") or {}
        raise UpstreamError(f"{settings.AWS_LAMBDA_ECG_READING}: {envelope} {err.get('code')}: {err.get('message')}")

    attempt.save()
    status = attempt.sinais["analyser_status"]
    expires = settings.PRESIGN_URL_EXPIRES
    urls = overlay_urls(attempt, expires)
    result_key = attempt.sinais.get("result_s3_key")
    return {**urls, "imagem_original_url": original_image_url(attempt, expires),
            "fidelidade": attempt.fidelidade, "image_type": attempt.image_type,
            "analyser_status": status, "warnings": warnings,
            "leads": attempt.lead_labels,
            "chosen_method": attempt.sinais.get("chosen_method"),
            "result_url": presign_get_url(settings.AWS_RESULTS_BUCKET, result_key, expires) if result_key else None}


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
