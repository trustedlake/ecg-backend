"""URL configuration for config project.

https://docs.djangoproject.com/en/5.2/topics/http/urls/
"""
from django.contrib import admin
from django.urls import include, path, re_path

from config.spa import SPA_PATH_PATTERN, spa_index

handler404 = 'config.spa.not_found'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/', include('exams.urls')),
    path('api/', include('uploads.urls')),
    # Por último: rotas do app web (SPA) caem no index.html; api/, admin/ e static/ não.
    re_path(SPA_PATH_PATTERN, spa_index),
]
