"""Integração Django com AWS S3 e as lambdas do contrato v1 (CONTRATO_LAMBDAS.md).

A orquestração da leitura e da interpretação entra na etapa B2 do plano
(docs/plano-migracao-v1.md); aqui ficam os clientes AWS e as presigned URLs.
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
    # O cliente precisa tolerar o tempo de resposta da lambda de leitura.
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
