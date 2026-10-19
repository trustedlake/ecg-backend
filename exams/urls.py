from django.urls import path

from .views import (
    AttemptAcceptView,
    AttemptCreateView,
    AttemptDetailView,
    AttemptReadView,
    AttemptRejectView,
    AttemptRetryView,
    ConfigView,
    ExamDetailView,
    ExamListCreateView,
    FeedbackCreateView,
    HealthView,
)

ATTEMPT = "exams/<uuid:exam_id>/attempts/<uuid:attempt_id>/"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("config/", ConfigView.as_view(), name="config"),
    path("exams/", ExamListCreateView.as_view(), name="exam-list"),
    path("exams/<uuid:exam_id>/", ExamDetailView.as_view(), name="exam-detail"),
    path(
        "exams/<uuid:exam_id>/attempts/",
        AttemptCreateView.as_view(),
        name="attempt-create",
    ),
    path(ATTEMPT, AttemptDetailView.as_view(), name="attempt-detail"),
    path(ATTEMPT + "read/", AttemptReadView.as_view(), name="attempt-read"),
    path(ATTEMPT + "accept/", AttemptAcceptView.as_view(), name="attempt-accept"),
    path(ATTEMPT + "reject/", AttemptRejectView.as_view(), name="attempt-reject"),
    path(ATTEMPT + "retry/", AttemptRetryView.as_view(), name="attempt-retry"),
    path(
        "attempts/<uuid:attempt_id>/feedback/",
        FeedbackCreateView.as_view(),
        name="attempt-feedback",
    ),
]
