import logging
import uuid
from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone
from rest_framework import pagination, status, serializers
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
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__patient"),
        id=attempt_id,
        exam__id=exam_id,
        exam__doctor=user,
    )
    return _recover_stale_attempt(attempt)


def _recover_stale_attempt(attempt):
    """Unstick an attempt left in fase_a_processando by an interrupted request.

    If a previous analysis already stored artifacts, the interrupted call was a refine:
    go back to the last reviewable version. Otherwise the analysis never finished: erro.
    """
    if attempt.status != ExamStatus.FASE_A_PROCESSANDO:
        return attempt
    limit = timezone.now() - timedelta(seconds=settings.STALE_PROCESSING_SECONDS)
    if attempt.updated_at > limit:
        return attempt
    saved = attempt.sinais if isinstance(attempt.sinais, dict) else {}
    new_status = ExamStatus.AGUARDANDO_APROVACAO if saved.get("result_s3_key") else ExamStatus.ERRO
    updated = ExamAttempt.objects.filter(
        pk=attempt.pk, status=ExamStatus.FASE_A_PROCESSANDO, updated_at__lte=limit,
    ).update(status=new_status, updated_at=timezone.now())
    if updated:
        logger.warning("Tentativa %s presa em processamento; marcada como %s", attempt.id, new_status)
        Exam.objects.filter(pk=attempt.exam_id, status=ExamStatus.FASE_A_PROCESSANDO).update(
            status=new_status, updated_at=timezone.now())
        attempt.refresh_from_db()
    return attempt


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
            calibracao={"speed_mm_s": float(serializer.validated_data["speed_mm_s"]),
                        "gain_mm_mv": float(serializer.validated_data["gain_mm_mv"]),
                        "source": "user_selected"},
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
    """POST process: one ecg-analyser Lambda."""

    permission_classes = [IsAuthenticated]

    def get(self, request, exam_id, attempt_id):
        """Return persisted state without triggering the Lambda a second time."""
        attempt = _attempt_or_404(request.user, exam_id, attempt_id)
        saved = attempt.sinais or {}
        keys = saved if isinstance(saved, dict) else {}
        def link(key):
            return (services.presign_get_url(settings.AWS_RESULTS_BUCKET, key,
                    settings.PRESIGN_URL_EXPIRES) if key else None)
        return Response({
            "status": attempt.status,
            "analyser_status": keys.get("analyser_status"),
            "warnings": keys.get("warnings", []),
            "fidelidade": attempt.fidelidade,
            "image_type": attempt.image_type,
            "overlay_trace_url": link(attempt.overlay_trace_s3_key),
            "overlay_signal_url": link(attempt.overlay_signal_s3_key),
            "working_image_url": link(keys.get("working_image_s3_key")),
            "result_url": link(keys.get("result_s3_key")),
        })

    def post(self, request, exam_id, attempt_id):
        attempt = _attempt_or_404(request.user, exam_id, attempt_id)
        exam = attempt.exam
        # CAS: two taps / network retries must never launch two Lambdas.
        updated = ExamAttempt.objects.filter(pk=attempt.pk, status=ExamStatus.CRIADO).update(
            status=ExamStatus.FASE_A_PROCESSANDO, updated_at=timezone.now())
        if not updated:
            return Response({"detail": "A tentativa já foi iniciada. Consulte o GET process/."},
                            status=status.HTTP_409_CONFLICT)
        attempt.status = ExamStatus.FASE_A_PROCESSANDO
        exam.status = ExamStatus.FASE_A_PROCESSANDO
        exam.save(update_fields=["status", "updated_at"])

        try:
            result = services.run_phase_a(attempt, exam)
        except Exception as exc:
            logger.exception("Fase A falhou para attempt %s", attempt.id)
            attempt.status = ExamStatus.ERRO
            exam.status = ExamStatus.ERRO
            attempt.save(update_fields=["status", "updated_at"])
            exam.save(update_fields=["status", "updated_at"])
            raise UpstreamAPIError("O processamento não pôde ser concluído. Consulte o estado do exame.")

        rejected = result.get("analyser_status", "").startswith("REJECTED_")
        attempt.status = ExamStatus.REJEITADO if rejected else ExamStatus.AGUARDANDO_APROVACAO
        exam.status = attempt.status
        attempt.save(update_fields=["status", "updated_at"])
        exam.save(update_fields=["status", "updated_at"])
        result["status"] = attempt.status
        return Response(result)



class PaintPointSerializer(serializers.Serializer):
    x=serializers.FloatField(min_value=0,max_value=1)
    y=serializers.FloatField(min_value=0,max_value=1)

class PaintStrokeSerializer(serializers.Serializer):
    lead=serializers.ChoiceField(choices=['I','II','III','aVR','aVL','aVF','V1','V2','V3','V4','V5','V6','II_longo'])
    mode=serializers.ChoiceField(choices=['include','exclude'])
    points=PaintPointSerializer(many=True,allow_empty=False,max_length=3000)

class PaintRequestSerializer(serializers.Serializer):
    coordinate_system=serializers.ChoiceField(choices=['normalized_image_v1'])
    lead=serializers.ChoiceField(choices=['I','II','III','aVR','aVL','aVF','V1','V2','V3','V4','V5','V6','II_longo'])
    strokes=PaintStrokeSerializer(many=True,allow_empty=False,max_length=200)

    def validate(self,data):
        import math
        if any(stroke['lead']!=data['lead'] for stroke in data['strokes']):
            raise serializers.ValidationError('Selecione uma única derivação por revisão')
        if sum(len(stroke['points']) for stroke in data['strokes']) > 12000:
            raise serializers.ValidationError('Número total de pontos excede o limite')
        if any(not math.isfinite(point['x']) or not math.isfinite(point['y'])
               for stroke in data['strokes'] for point in stroke['points']):
            raise serializers.ValidationError('Coordenadas devem ser números finitos')
        return data

class AttemptRefineView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, exam_id, attempt_id):
        serializer = PaintRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Acquire a brief DB lock before remote invocation. Never hold this lock
        # while waiting on Lambda: block other refine/approve requests by state.
        with transaction.atomic():
            attempt = _attempt_or_404(request.user, exam_id, attempt_id)
            attempt = ExamAttempt.objects.select_for_update().get(pk=attempt.pk)
            if attempt.status != ExamStatus.AGUARDANDO_APROVACAO:
                return Response({'detail': 'Tentativa em processamento ou não revisável.'}, status=409)
            attempt.status = ExamStatus.FASE_A_PROCESSANDO
            attempt.save(update_fields=['status', 'updated_at'])
        try:
            result = services.run_refine(attempt, attempt.exam,
                serializer.validated_data['lead'], serializer.validated_data['strokes'])
            return Response(result)
        except services.UpstreamError:
            logger.exception("Refinamento falhou para attempt %s", attempt.id)
            raise UpstreamAPIError("Falha no refinamento. O resultado anterior foi preservado.")
        finally:
            # Preserve last verified version even on Lambda errors/timeouts.
            with transaction.atomic():
                ExamAttempt.objects.filter(pk=attempt.pk, status=ExamStatus.FASE_A_PROCESSANDO).update(
                    status=ExamStatus.AGUARDANDO_APROVACAO, updated_at=timezone.now())


class AttemptApproveView(APIView):
    """POST approve: review extracted trace, without clinical diagnosis."""

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
                {"detail": "Extração não concluída — sem sinais para revisão."},
                status=status.HTTP_409_CONFLICT,
            )

        updated = ExamAttempt.objects.filter(pk=attempt.pk, status=ExamStatus.AGUARDANDO_APROVACAO).update(
            status=ExamStatus.FASE_B_PROCESSANDO, updated_at=timezone.now())
        if not updated:
            return Response({"detail": "A tentativa foi alterada por outra requisição."}, status=409)
        attempt.status = ExamStatus.FASE_B_PROCESSANDO
        exam.status = ExamStatus.FASE_B_PROCESSANDO
        exam.save(update_fields=["status", "updated_at"])

        try:
            report, urls = services.run_phase_b(attempt, exam)
        except Exception as exc:
            logger.exception("Fase B falhou para attempt %s", attempt.id)
            attempt.status = ExamStatus.ERRO
            exam.status = ExamStatus.ERRO
            attempt.save(update_fields=["status", "updated_at"])
            exam.save(update_fields=["status", "updated_at"])
            raise UpstreamAPIError("O processamento não pôde ser concluído. Consulte o estado do exame.")

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
        updated = ExamAttempt.objects.filter(pk=attempt.pk, status=ExamStatus.AGUARDANDO_APROVACAO).update(
            status=ExamStatus.REJEITADO, updated_at=timezone.now())
        if not updated:
            return Response({"detail": "A tentativa foi alterada por outra requisição."}, status=409)
        attempt.status = ExamStatus.REJEITADO
        exam.status = ExamStatus.REJEITADO
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