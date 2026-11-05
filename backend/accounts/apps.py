from django.apps import AppConfig
from django.db.models.signals import pre_migrate


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


class AccountsConfig(AppConfig):
    name = "accounts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        pre_migrate.connect(ensure_singularity_schema, dispatch_uid="ensure_singularity_schema")
