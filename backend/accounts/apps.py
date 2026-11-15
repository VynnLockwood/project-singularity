import logging

from django.apps import AppConfig
from django.db.models.signals import post_migrate, pre_migrate

logger = logging.getLogger("accounts")


def ensure_singularity_schema(sender, using="default", **kwargs):
    """Create the `singularity` schema if it is missing (dev and test bootstrap).

    In production db/init creates it, and the app role has no CREATE on the
    database, so we only issue CREATE SCHEMA when the schema is really absent.
    """
    from django.db import connections

    conn = connections[using]
    if conn.vendor != "postgresql":
        return
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'singularity'")
        if cur.fetchone() is None:
            cur.execute("CREATE SCHEMA singularity")


def seed_admins_after_migrate(sender, using="default", **kwargs):
    """Create or promote the configured seed admins once the tables exist. Never aborts migrate: any error is
    logged and the migration carries on (fix the config and migrate again)."""
    if using != "default":
        return
    try:
        from config.siconfig import get_config

        from . import services

        services.ensure_seed_admins(get_config()["auth"])
    except Exception:
        logger.exception("seeding admins after migrate failed; migrate continues")


class AccountsConfig(AppConfig):
    name = "accounts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        pre_migrate.connect(ensure_singularity_schema, dispatch_uid="ensure_singularity_schema")
        post_migrate.connect(seed_admins_after_migrate, sender=self, dispatch_uid="ensure_seed_admins")
