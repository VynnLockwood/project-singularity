from django.apps import AppConfig


class SearchConfig(AppConfig):
    name = "search"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
