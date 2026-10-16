from django.urls import path

from .views import (
    AttemptCreateView,
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
        "attempts/<uuid:attempt_id>/feedback/",
        FeedbackCreateView.as_view(),
        name="attempt-feedback",
    ),
]
