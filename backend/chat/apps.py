from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "chat"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
