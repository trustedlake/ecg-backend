import logging
import uuid

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import pagination, status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings

from . import services
from .models import Exam, ExamAttempt, ExamStatus, Feedback
from .serializers import (
    AttemptCreateSerializer,
    ExamCreateSerializer,
    ExamDetailSerializer,
    ExamSerializer,
    FeedbackSerializer,
    ReportSerializer,
)

logger = logging.getLogger(__name__)


class UpstreamAPIError(APIException):
    status_code = 502
    default_detail = "Falha no pipeline de processamento."
    default_code = "upstream_error"


class ExamPagination(pagination.PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _attempt_or_404(user, exam_id, attempt_id):
    return get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__patient"),
        id=attempt_id,
        exam__id=exam_id,
        exam__doctor=user,
    )


class ExamListCreateView(APIView):
    """GET /api/exams/ — lista (com paginação) · POST /api/exams/ — cria."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        exams = (
            Exam.objects.filter(doctor=request.user)
            .select_related("patient")
            .prefetch_related("attempts", "attempts__report")
            .order_by("-created_at")
        )
        paginator = ExamPagination()
        page = paginator.paginate_queryset(exams, request, view=self)
        if page is not None:
            data = ExamSerializer(page, many=True).data
            return paginator.get_paginated_response(data)
        return Response(ExamSerializer(list(exams), many=True).data)

    def post(self, request):
        serializer = ExamCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        exam = serializer.save()
        return Response(ExamSerializer(exam).data, status=status.HTTP_201_CREATED)


class ExamDetailView(APIView):
    """GET /api/exams/<id>/ — detalhe + tentativas + report + URLs presigned."""

    permission_classes = [IsAuthenticated]

    def get(self, request, exam_id):
        exam = get_object_or_404(
            Exam.objects.select_related("patient").prefetch_related(
                "attempts", "attempts__report"
            ),
            id=exam_id,
            doctor=request.user,
        )
        return Response(ExamDetailSerializer(exam, context={"request": request}).data)


class AttemptCreateView(APIView):
    """POST /api/exams/<id>/attempts/ — cria tentativa + presigned PUT URL."""

    permission_classes = [IsAuthenticated]

    def post(self, request, exam_id):
        exam = get_object_or_404(Exam.objects.select_related("patient"), id=exam_id, doctor=request.user)
        serializer = AttemptCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        attempt_id = uuid.uuid4()
        timestamp = timezone.now().strftime("%Y-%m-%dT%H-%M-%S")
        key = f"{request.user.username}/{timestamp}/{exam.id}/{attempt_id}/original.{serializer.validated_data['_ext']}"
        attempt = ExamAttempt.objects.create(
            id=attempt_id,
            exam=exam,
            status=ExamStatus.CRIADO,
            imagem_s3_key=key,
        )
        # Exam ganha uma nova tentativa: volta ao estado inicial do fluxo.
        if exam.status in (
            ExamStatus.REJEITADO,
            ExamStatus.ERRO,
            ExamStatus.CONCLUIDO,
        ):
            exam.status = ExamStatus.CRIADO
            exam.save(update_fields=["status", "updated_at"])

        upload_url = services.presign_put_url(
            settings.AWS_IMAGES_BUCKET,
            key,
            settings.PRESIGN_URL_EXPIRES,
            content_type=serializer.validated_data.get("content_type") or None,
        )
        return Response(
            {
                "attempt_id": str(attempt.id),
                "upload_url": upload_url,
                "imagem_s3_key": attempt.imagem_s3_key,
            },
            status=status.HTTP_201_CREATED,
        )


class AttemptProcessView(APIView):
    """POST .../attempts/<id>/process/ — Fase A (lambda-1 + lambda-2)."""

    permission_classes = [IsAuthenticated]

    def post(self, request, exam_id, attempt_id):
        attempt = _attempt_or_404(request.user, exam_id, attempt_id)
        exam = attempt.exam
        if attempt.status != ExamStatus.CRIADO:
            return Response(
                {"detail": f"Tentativa em estado {attempt.status} — processar exige 'criado'."},
                status=status.HTTP_409_CONFLICT,
            )

        attempt.status = ExamStatus.FASE_A_PROCESSANDO
        exam.status = ExamStatus.FASE_A_PROCESSANDO
        attempt.save(update_fields=["status", "updated_at"])
        exam.save(update_fields=["status", "updated_at"])

        try:
            result = services.run_phase_a(attempt, exam)
        except Exception as exc:
            logger.exception("Fase A falhou para attempt %s", attempt.id)
            attempt.status = ExamStatus.ERRO
            exam.status = ExamStatus.ERRO
            attempt.save(update_fields=["status", "updated_at"])
            exam.save(update_fields=["status", "updated_at"])
            raise UpstreamAPIError(str(exc))

        attempt.status = ExamStatus.AGUARDANDO_APROVACAO
        exam.status = ExamStatus.AGUARDANDO_APROVACAO
        attempt.save(update_fields=["status", "updated_at"])
        exam.save(update_fields=["status", "updated_at"])
        result["status"] = attempt.status
        return Response(result)


class AttemptApproveView(APIView):
    """POST /api/exams/<id>/attempts/<aid>/aprovar/ — Fase B (lambda-3)."""

    permission_classes = [IsAuthenticated]

    def post(self, request, exam_id, attempt_id):
        attempt = _attempt_or_404(request.user, exam_id, attempt_id)
        exam = attempt.exam
        if attempt.status != ExamStatus.AGUARDANDO_APROVACAO:
            return Response(
                {"detail": f"Estado {attempt.status} — aprovar exige 'aguardando_aprovacao'."},
                status=status.HTTP_409_CONFLICT,
            )
        if not attempt.sinais:
            return Response(
                {"detail": "Fase A não concluída — sem sinais salvos para diagnosticar."},
                status=status.HTTP_409_CONFLICT,
            )

        attempt.status = ExamStatus.FASE_B_PROCESSANDO
        exam.status = ExamStatus.FASE_B_PROCESSANDO
        attempt.save(update_fields=["status", "updated_at"])
        exam.save(update_fields=["status", "updated_at"])

        try:
            report, urls = services.run_phase_b(attempt, exam)
        except Exception as exc:
            logger.exception("Fase B falhou para attempt %s", attempt.id)
            attempt.status = ExamStatus.ERRO
            exam.status = ExamStatus.ERRO
            attempt.save(update_fields=["status", "updated_at"])
            exam.save(update_fields=["status", "updated_at"])
            raise UpstreamAPIError(str(exc))

        attempt.status = ExamStatus.CONCLUIDO
        exam.status = ExamStatus.CONCLUIDO
        attempt.save(update_fields=["status", "updated_at"])
        exam.save(update_fields=["status", "updated_at"])

        return Response({**ReportSerializer(report).data, **urls})


class AttemptRejectView(APIView):
    """POST /api/exams/<id>/attempts/<aid>/rejeitar/."""

    permission_classes = [IsAuthenticated]

    def post(self, request, exam_id, attempt_id):
        attempt = _attempt_or_404(request.user, exam_id, attempt_id)
        exam = attempt.exam
        if attempt.status != ExamStatus.AGUARDANDO_APROVACAO:
            return Response(
                {"detail": f"Estado {attempt.status} — rejeitar exige 'aguardando_aprovacao'."},
                status=status.HTTP_409_CONFLICT,
            )
        attempt.status = ExamStatus.REJEITADO
        exam.status = ExamStatus.REJEITADO
        attempt.save(update_fields=["status", "updated_at"])
        exam.save(update_fields=["status", "updated_at"])
        return Response({"status": attempt.status})


class FeedbackCreateView(APIView):
    """POST /api/attempts/<attempt_id>/feedback/ — crítica do médico."""

    permission_classes = [IsAuthenticated]

    def post(self, request, attempt_id):
        attempt = get_object_or_404(
            ExamAttempt, id=attempt_id, exam__doctor=request.user
        )
        serializer = FeedbackSerializer(
            data=request.data, context={"request": request, "attempt": attempt}
        )
        serializer.is_valid(raise_exception=True)
        feedback = serializer.save()
        return Response(
            FeedbackSerializer(feedback).data, status=status.HTTP_201_CREATED
        )