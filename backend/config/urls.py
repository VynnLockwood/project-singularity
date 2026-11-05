from django.urls import include, path

from config.views import health

urlpatterns = [
    path("api/health", health),
    path("api/", include("accounts.urls")),
]
