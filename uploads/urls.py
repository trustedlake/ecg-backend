from django.urls import path

from .views import UploadBatchConfirmView, UploadBatchCreateView

urlpatterns = [
    path("uploads/", UploadBatchCreateView.as_view(), name="upload-batch-create"),
    path(
        "uploads/<uuid:batch_id>/confirm/",
        UploadBatchConfirmView.as_view(),
        name="upload-batch-confirm",
    ),
]
