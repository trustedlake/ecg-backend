import logging
import uuid

from django.conf import settings
from django.db import connection
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import pagination, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services, tasks
from .models import SUPPORTED_LAYOUTS, Exam, ExamAttempt, ExamStatus
from .serializers import (
    GAIN_CHOICES,
    SPEED_CHOICES,
    AttemptCreateSerializer,
    AttemptSerializer,
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
    """GET /api/exams/: list (paginated) · POST /api/exams/: create."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        exams = (
            Exam.objects.filter(doctor=request.user)
            .select_related("patient")
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
    """GET /api/exams/<id>/: detail + attempts + report + presigned URLs."""

    permission_classes = [IsAuthenticated]

    def get(self, request, exam_id):
        exam = get_object_or_404(
            Exam.objects.select_related("patient").prefetch_related(
                "attempts", "attempts__report"
            ),
            id=exam_id,
            doctor=request.user,
        )
        for attempt in exam.attempts.all():
            services.recover_stale(attempt)
        return Response(ExamDetailSerializer(exam, context={"request": request}).data)


class AttemptCreateView(APIView):
    """POST /api/exams/<id>/attempts/: creates an attempt + presigned PUT URL."""

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
            status=ExamStatus.CREATED,
            image_s3_key=key,
            layout=data["layout"],
            calibration={"speed_mm_s": data["speed_mm_s"], "gain_mm_mv": data["gain_mm_mv"]},
            test_scenario=data.get("test_scenario"),
        )
        # The exam mirrors the latest attempt.
        exam.status = ExamStatus.CREATED
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
    """POST /api/attempts/<attempt_id>/feedback/: doctor feedback."""

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


def _invalid_state(attempt):
    return Response(
        {"detail": f"Action not allowed in the attempt's current state ({attempt.status}).",
         "code": "invalid_state"},
        status=status.HTTP_409_CONFLICT,
    )


class AttemptActionView(APIView):
    """Base of the /api/exams/<exam_id>/attempts/<attempt_id>/... routes: scoped per doctor (404)."""

    permission_classes = [IsAuthenticated]

    def get_attempt(self, request, exam_id, attempt_id):
        attempt = get_object_or_404(
            ExamAttempt.objects.select_related("exam__patient", "report"),
            id=attempt_id, exam_id=exam_id, exam__doctor=request.user,
        )
        return services.recover_stale(attempt)

    def respond(self, attempt, code=status.HTTP_200_OK):
        attempt = ExamAttempt.objects.select_related("exam", "report").get(pk=attempt.pk)
        return Response(AttemptSerializer(attempt).data, status=code)

    def start_phase(self, attempt, phase, from_statuses):
        """CAS into the phase's processing state + dispatch. Two taps: the second one gets 409."""
        processing = services.PHASES[phase][0]
        if not services.transition(attempt, from_statuses, processing):
            return _invalid_state(attempt)
        tasks.dispatch(phase, attempt)
        return self.respond(attempt, status.HTTP_202_ACCEPTED)


class AttemptDetailView(AttemptActionView):
    """GET .../attempts/<id>/: Attempt object (the app's polling endpoint)."""

    def get(self, request, exam_id, attempt_id):
        return self.respond(self.get_attempt(request, exam_id, attempt_id))


class AttemptReadView(AttemptActionView):
    """POST .../attempts/<id>/read/: starts the reading (after the photo PUT)."""

    def post(self, request, exam_id, attempt_id):
        attempt = self.get_attempt(request, exam_id, attempt_id)
        return self.start_phase(attempt, "read", ExamStatus.CREATED)


class AttemptAcceptView(AttemptActionView):
    """POST .../attempts/<id>/accept/: the user approved the reading -> interpretation."""

    def post(self, request, exam_id, attempt_id):
        attempt = self.get_attempt(request, exam_id, attempt_id)
        return self.start_phase(attempt, "interpret", ExamStatus.AWAITING_VALIDATION)


class AttemptRejectView(AttemptActionView):
    """POST .../attempts/<id>/reject/: the user rejected the reading (new photo = new attempt)."""

    def post(self, request, exam_id, attempt_id):
        attempt = self.get_attempt(request, exam_id, attempt_id)
        if not services.transition(attempt, ExamStatus.AWAITING_VALIDATION, ExamStatus.USER_REJECTED):
            return _invalid_state(attempt)
        return self.respond(attempt)


class AttemptRetryView(AttemptActionView):
    """POST .../attempts/<id>/retry/: reruns the failed phase if the error is retryable."""

    def post(self, request, exam_id, attempt_id):
        attempt = self.get_attempt(request, exam_id, attempt_id)
        if not services.attempt_flags(attempt)["can_retry"]:
            return _invalid_state(attempt)
        phase = "read" if attempt.status == ExamStatus.READING_ERROR else "interpret"
        from_status = attempt.status
        if attempt.test_scenario and phase in attempt.test_scenario:
            # A staff-forced scenario must not repeat itself: the retry runs the real path.
            remaining = {k: v for k, v in attempt.test_scenario.items() if k != phase}
            attempt.test_scenario = remaining or None
            attempt.save(update_fields=["test_scenario"])
        return self.start_phase(attempt, phase, from_status)


class ConfigView(APIView):
    """GET /api/config/: options for the attempt form."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "layouts": [layout.value for layout in SUPPORTED_LAYOUTS],
            "calibration": {
                "speed_mm_s": SPEED_CHOICES,
                "gain_mm_mv": GAIN_CHOICES,
                "default": {"speed_mm_s": 25, "gain_mm_mv": 10},
            },
            "test_scenarios_enabled": bool(settings.ECG_TEST_SCENARIOS_ENABLED and request.user.is_staff),
        })


class HealthView(APIView):
    """GET /api/health/: no auth; used by the deploy health check and the smoke test."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("Health check: database unavailable")
            return Response(
                {"status": "error", "db": "error", "version": settings.APP_VERSION},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ok", "db": "ok", "version": settings.APP_VERSION})
