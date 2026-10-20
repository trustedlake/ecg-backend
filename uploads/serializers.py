from rest_framework import serializers

VALID_IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "webp", "tiff"}


def _ext_from(filename, content_type):
    if filename:
        dot = filename.rfind(".")
        ext = filename[dot + 1:].lower() if dot != -1 else filename.lower()
    elif content_type == "image/jpeg":
        ext = "jpg"
    elif content_type and content_type.startswith("image/"):
        ext = content_type.split("/")[1] or "jpg"
    else:
        ext = "jpg"
    return ext


class UploadFileRequestSerializer(serializers.Serializer):
    """One item of the list sent to POST /api/uploads/: metadata of the file
    the frontend wants to upload (the binary goes later, straight to S3 via
    presigned PUT)."""

    filename = serializers.CharField(required=False, allow_blank=True, default="")
    content_type = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        ext = _ext_from(attrs.get("filename", ""), attrs.get("content_type", ""))
        if ext not in VALID_IMAGE_EXTS:
            raise serializers.ValidationError(
                {"filename": f"Unsupported image extension: '{ext}'."}
            )
        attrs["_ext"] = ext
        return attrs


class UploadBatchCreateSerializer(serializers.Serializer):
    files = UploadFileRequestSerializer(many=True)

    def validate_files(self, value):
        if not value:
            raise serializers.ValidationError("Send at least one file.")
        return value
