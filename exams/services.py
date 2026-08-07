"""Integração AWS: invocação das 3 lambdas (2 fases) + presigned URLs do S3.

Payloads seguem os handlers reais (fonte da verdade):
- lambda-1: ecg-lambdas/lambdas/lambda_1_gridline_and_trace/handler.py
  entrada: {exame_id, imagem_s3_key} (linhas 46-47)
  saída:   {exame_id, image_type, image_type_motivo, ecg_colors,
            mascara_traco_s3_key, imagem_original_s3_key, calibracao} (89-97)
- lambda-2: ecg-lambdas/lambdas/lambda_2_signal_extraction/handler.py
  entrada: {exame_id, mascara_traco_s3_key, imagem_original_s3_key,
            calibracao, ecg_layout} (linhas 60-64)
  saída:   {exame_id, ecg_layout, lead_labels, rhythm_index, paineis,
            ecg_pintado_traco_s3_key, ecg_pintado_sinal_s3_key, sinais,
            fidelidade_por_derivacao, fidelidade, calibracao} (142-154)
- lambda-3: ecg-lambdas/lambdas/lambda_3_diagnosis/handler.py
  entrada: {exame_id, sinais, lead_labels, rhythm_index, calibracao,
            fidelidade, sexo_feminino} (linhas 30-36; sexo_feminino opcional)
  saída:   {exame_id, ritmo, eixo_graus, sokolow_mm, derivacoes, indicadores,
            achados, conclusao, confiabilidade, versao_codigo} (67-78)
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
    # lambda-2 tem timeout de 300s — o client boto3 precisa ler a resposta por
    # mais tempo do que isso, senão desiste antes da lambda responder.
    return _session().client(
        "lambda",
        config=Config(
            read_timeout=330,
            connect_timeout=10,
            retries={"max_attempts": 2},
        ),
    )


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
    return _session().client("s3").generate_presigned_url(
        "put_object", Params=params, ExpiresIn=expires_in
    )


def presign_get_url(bucket, key, expires_in):
    """Presigned GET sob demanda — URLs nunca são persistidas no banco."""
    return _session().client("s3").generate_presigned_url(
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
    """Fase A: lambda-1 (gridline-and-trace) → lambda-2 (signal-extraction),
    em sequência síncrona, sem gate no meio. Persiste na attempt tudo que as
    lambdas devolvem; levanta UpstreamError em qualquer falha.

    Retorna o dict de resposta do endpoint (overlays + fidelidade + image_type).
    """
    expires = settings.PRESIGN_URL_EXPIRES

    out1 = invoke_lambda(settings.AWS_LAMBDA_GRIDLINE_TRACE, {
        "exame_id": str(exam.id),
        "imagem_s3_key": attempt.imagem_s3_key,
    })
    attempt.image_type = out1["image_type"]
    attempt.calibracao = out1["calibracao"]
    attempt.mascara_s3_key = out1["mascara_traco_s3_key"]
    attempt.save()

    out2 = invoke_lambda(settings.AWS_LAMBDA_SIGNAL_EXTRACTION, {
        "exame_id": str(exam.id),
        "mascara_traco_s3_key": out1["mascara_traco_s3_key"],
        "imagem_original_s3_key": out1["imagem_original_s3_key"],
        "calibracao": out1["calibracao"],
        "ecg_layout": exam.ecg_layout,
    })
    attempt.overlay_trace_s3_key = out2["ecg_pintado_traco_s3_key"]
    attempt.overlay_signal_s3_key = out2["ecg_pintado_sinal_s3_key"]
    attempt.sinais = out2["sinais"]
    attempt.lead_labels = out2["lead_labels"]
    attempt.rhythm_index = out2["rhythm_index"]
    attempt.fidelidade = out2["fidelidade"]
    attempt.fidelidade_por_derivacao = out2["fidelidade_por_derivacao"]
    attempt.save()

    urls = overlay_urls(attempt, expires)
    return {
        "overlay_trace_url": urls["overlay_trace_url"],
        "overlay_signal_url": urls["overlay_signal_url"],
        "fidelidade": attempt.fidelidade,
        "image_type": attempt.image_type,
        "status": attempt.status,
    }


def run_phase_b(attempt, exam):
    """Fase B: lambda-3 (diagnosis) com os dados já salvos na tentativa
    (sexo_feminino fica de fora — decisão fechada). Cria o Report 1:1.

    Retorna o dict de resposta (report serializado + presigned URLs das 3
    imagens).
    """
    from .models import Report

    expires = settings.PRESIGN_URL_EXPIRES

    payload = {
        "exame_id": str(exam.id),
        "sinais": attempt.sinais,
        "lead_labels": attempt.lead_labels,
        "calibracao": attempt.calibracao,
        "fidelidade": attempt.fidelidade,
    }
    if attempt.rhythm_index is not None:
        payload["rhythm_index"] = attempt.rhythm_index

    out3 = invoke_lambda(settings.AWS_LAMBDA_DIAGNOSIS, payload)

    report, _ = Report.objects.update_or_create(
        attempt=attempt,
        defaults={
            "ritmo": out3.get("ritmo"),
            "eixo_graus": out3.get("eixo_graus"),
            "sokolow_mm": out3.get("sokolow_mm"),
            "derivacoes": out3.get("derivacoes"),
            "indicadores": out3.get("indicadores"),
            "achados": out3.get("achados"),
            "conclusao": out3.get("conclusao"),
            "confiabilidade": out3.get("confiabilidade"),
            "versao_codigo": out3.get("versao_codigo"),
        },
    )

    urls = {
        "imagem_original_url": original_image_url(attempt, expires),
        **overlay_urls(attempt, expires),
    }
    return report, urls
