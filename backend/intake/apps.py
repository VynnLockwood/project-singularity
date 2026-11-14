from django.apps import AppConfig


class IntakeConfig(AppConfig):
    name = "intake"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
