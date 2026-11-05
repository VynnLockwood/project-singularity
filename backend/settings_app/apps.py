from django.apps import AppConfig
from django.db.models.signals import post_migrate


def seed_rules(sender, **kwargs):
    from .models import Rule  # noqa: F401  (ensures the table exists)
    from .services import seed_default_rules

    seed_default_rules()


class SettingsAppConfig(AppConfig):
    name = "settings_app"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        post_migrate.connect(seed_rules, sender=self, dispatch_uid="seed_default_rules")
