import uuid

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from exams import services

from .models import UploadBatch, UploadedFile
from .serializers import UploadBatchCreateSerializer


class UploadBatchCreateView(APIView):
    """POST /api/uploads/ — registra o lote (não confirmado) e devolve uma
    presigned PUT URL por arquivo. O upload do binário é direto pro S3,
    nunca passa pelo Django (mesmo padrão de AttemptCreateView)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = UploadBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        batch = UploadBatch.objects.create(user=request.user)
        timestamp = timezone.now().strftime("%Y-%m-%dT%H-%M-%S")

        files = []
        for item in serializer.validated_data["files"]:
            file_id = uuid.uuid4()
            key = (
                f"device-uploads/{request.user.username}/{timestamp}/"
                f"{batch.id}/{file_id}.{item['_ext']}"
            )
            uploaded = UploadedFile.objects.create(
                id=file_id,
                batch=batch,
                s3_key=key,
                content_type=item.get("content_type") or None,
                nome_original=item.get("filename") or None,
            )

            upload_url = services.presign_put_url(
                settings.AWS_IMAGES_BUCKET,
                key,
                settings.PRESIGN_URL_EXPIRES,
                content_type=item.get("content_type") or None,
            )
            files.append(
                {
                    "file_id": str(uploaded.id),
                    "s3_key": key,
                    "upload_url": upload_url,
                }
            )

        return Response(
            {"batch_id": str(batch.id), "files": files},
            status=status.HTTP_201_CREATED,
        )


class UploadBatchConfirmView(APIView):
    """POST /api/uploads/<batch_id>/confirmar/ — só vira o flag; não
    reenvia nem reprocessa nenhum arquivo."""

    permission_classes = [IsAuthenticated]

    def post(self, request, batch_id):
        batch = get_object_or_404(UploadBatch, id=batch_id, user=request.user)
        if not batch.confirmado:
            batch.confirmado = True
            batch.confirmado_em = timezone.now()
            batch.save(update_fields=["confirmado", "confirmado_em"])
        return Response({"batch_id": str(batch.id), "confirmado": batch.confirmado})
