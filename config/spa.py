"""Catch-all da SPA (export web do Expo em settings.FRONTEND_DIST) e 404 JSON da API."""

from django.conf import settings
from django.http import FileResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.http import require_GET
from django.views.defaults import page_not_found

# api/, admin/ e static/ ficam fora do catch-all: um 404 neles continua sendo 404 de verdade
# (e o APPEND_SLASH continua redirecionando /api/health -> /api/health/).
SPA_PATH_PATTERN = r"^(?!(?:api|admin|static)(?:/|$)).*$"


@require_GET
def spa_index(request):
    index = settings.FRONTEND_DIST / "index.html"
    if not index.is_file():
        return HttpResponseNotFound(
            "App web não incluído nesta imagem (frontend_dist/index.html ausente).",
            content_type="text/plain; charset=utf-8",
        )
    response = FileResponse(index.open("rb"), content_type="text/html; charset=utf-8")
    # O index.html aponta para bundles com hash; não pode ficar em cache após um deploy.
    response["Cache-Control"] = "no-cache"
    return response


def not_found(request, exception=None):
    if request.path.startswith("/api/"):
        return JsonResponse({"detail": "Not found."}, status=404)
    return page_not_found(request, exception)
