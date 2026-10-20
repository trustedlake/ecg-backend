"""SPA catch-all (Expo web export in settings.FRONTEND_DIST) and the API JSON 404."""

from django.conf import settings
from django.http import FileResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.http import require_GET
from django.views.defaults import page_not_found

# api/, admin/ and static/ stay out of the catch-all: a 404 there is still a real 404
# (e o APPEND_SLASH continua redirecionando /api/health -> /api/health/).
SPA_PATH_PATTERN = r"^(?!(?:api|admin|static)(?:/|$)).*$"


@require_GET
def spa_index(request):
    index = settings.FRONTEND_DIST / "index.html"
    if not index.is_file():
        return HttpResponseNotFound(
            "Web app not included in this image (frontend_dist/index.html missing).",
            content_type="text/plain; charset=utf-8",
        )
    response = FileResponse(index.open("rb"), content_type="text/html; charset=utf-8")
    # index.html points to hashed bundles; it must not stay cached after a deploy.
    response["Cache-Control"] = "no-cache"
    return response


def not_found(request, exception=None):
    if request.path.startswith("/api/"):
        return JsonResponse({"detail": "Not found."}, status=404)
    return page_not_found(request, exception)
