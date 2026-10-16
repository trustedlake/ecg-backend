import logging
import uuid

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import pagination, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings

from . import services
from .models import Exam, ExamAttempt, ExamStatus
from .serializers import (
    AttemptCreateSerializer,
    ExamCreateSerializer,
    ExamDetailSerializer,
    ExamSerializer,
    FeedbackSerializer,
)

logger = logging.getLogger(__name__)


class ExamPagination(pagination.PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


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
        serializer = AttemptCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        attempt_id = uuid.uuid4()
        timestamp = timezone.now().strftime("%Y-%m-%dT%H-%M-%S")
        key = f"{request.user.username}/{timestamp}/{exam.id}/{attempt_id}/original.{data['_ext']}"
        attempt = ExamAttempt.objects.create(
            id=attempt_id,
            exam=exam,
            status=ExamStatus.CRIADO,
            imagem_s3_key=key,
            layout=data["layout"],
            calibracao={"speed_mm_s": data["speed_mm_s"], "gain_mm_mv": data["gain_mm_mv"]},
            test_scenario=data.get("test_scenario"),
        )
        # O exame espelha a tentativa mais recente.
        exam.status = ExamStatus.CRIADO
        exam.save(update_fields=["status", "updated_at"])

        upload_url = services.presign_put_url(
            settings.AWS_IMAGES_BUCKET,
            key,
            settings.PRESIGN_URL_EXPIRES,
            content_type=data["content_type"],
        )
        return Response(
            {
                "attempt_id": str(attempt.id),
                "upload_url": upload_url,
                "upload_headers": {"Content-Type": data["content_type"]},
            },
            status=status.HTTP_201_CREATED,
        )


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