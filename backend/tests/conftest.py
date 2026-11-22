import hashlib
import math
import re

import pytest

from . import pgsetup

MASTER_DB_NAME = "master_test"


# The post_migrate seed would put the example admin from singularity.yaml into every test database.
# Tests call ensure_seed_admins themselves.
from django.apps import apps  # noqa: E402
from django.db.models.signals import post_migrate  # noqa: E402

post_migrate.disconnect(dispatch_uid="ensure_seed_admins", sender=apps.get_app_config("accounts"))


def _default_access(sender, instance, created, raw=False, **kwargs):
    """Tests only: a new non-admin user gets a `*` grant at its global role (today's reach before grants)."""
    from accounts.models import Role, SchemaGrant

    if created and not raw and instance.role != Role.ADMIN:
        SchemaGrant.objects.get_or_create(user=instance, schema="*", defaults={"role": instance.role})


@pytest.fixture(autouse=True)
def _default_schema_access(request):
    """Per-database access fails closed; the older tests predate grants, so their users get one (see above).

    Tests marked `no_default_access` get none and set grants themselves. Installed here only, never in app code.
    """
    if request.node.get_closest_marker("no_default_access"):
        yield
        return
    from django.db.models.signals import post_save

    from accounts.models import User

    uid = "tests_default_schema_access"
    post_save.connect(_default_access, sender=User, dispatch_uid=uid)
    try:
        yield
    finally:
        post_save.disconnect(sender=User, dispatch_uid=uid)


@pytest.fixture(scope="session")
def master_db():
    if not pgsetup.ADMIN:
        pytest.skip("TEST_PG_ADMIN_USER not set (needs a Postgres superuser)")
    pgsetup.create_scratch_db(MASTER_DB_NAME)
    yield MASTER_DB_NAME
    pgsetup.drop_db(MASTER_DB_NAME)


@pytest.fixture
def master(master_db, settings, tmp_path):
    """Point the app at a scratch `master` schema (real restricted roles), empty for each test."""
    settings.MASTER_DB = {
        "host": pgsetup.HOST, "port": pgsetup.PORT, "dbname": master_db,
        "ro_user": "singularity_master_ro", "ro_password": pgsetup.PASSWORDS["singularity_master_ro"],
        "rw_user": "singularity_master_rw", "rw_password": pgsetup.PASSWORDS["singularity_master_rw"],
    }
    settings.BACKUP_DIR = str(tmp_path / "backups")
    pgsetup.wipe_master(master_db)
    yield master_db
    pgsetup.wipe_master(master_db)


@pytest.fixture(autouse=True)
def _script_dir(settings, tmp_path):
    """Script uploads never touch the real /scripts (it does not exist, or is not writable, on CI)."""
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


def fake_vector(text: str) -> list[float]:
    """Deterministic stand-in for the model: hashed bag of words, unit length. Shared words = similar."""
    v = [0.0] * 384
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 384] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture
def embed(settings, monkeypatch):
    from search import embedder

    settings.EMBED_URL = "http://embed.test"
    calls = []

    def fake(texts, kind):
        calls.append((kind, list(texts)))
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(embedder, "embed", fake)
    return calls
