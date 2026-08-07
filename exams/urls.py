from django.urls import path

from .views import (
    AttemptApproveView,
    AttemptCreateView,
    AttemptProcessView,
    AttemptRejectView,
    ExamDetailView,
    ExamListCreateView,
    FeedbackCreateView,
)

urlpatterns = [
    path("exams/", ExamListCreateView.as_view(), name="exam-list"),
    path("exams/<uuid:exam_id>/", ExamDetailView.as_view(), name="exam-detail"),
    path(
        "exams/<uuid:exam_id>/attempts/",
        AttemptCreateView.as_view(),
        name="attempt-create",
    ),
    path(
        "exams/<uuid:exam_id>/attempts/<uuid:attempt_id>/process/",
        AttemptProcessView.as_view(),
        name="attempt-process",
    ),
    path(
        "exams/<uuid:exam_id>/attempts/<uuid:attempt_id>/aprovar/",
        AttemptApproveView.as_view(),
        name="attempt-aprovar",
    ),
    path(
        "exams/<uuid:exam_id>/attempts/<uuid:attempt_id>/rejeitar/",
        AttemptRejectView.as_view(),
        name="attempt-rejeitar",
    ),
    path(
        "attempts/<uuid:attempt_id>/feedback/",
        FeedbackCreateView.as_view(),
        name="attempt-feedback",
    ),
]