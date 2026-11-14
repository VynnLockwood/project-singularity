import hashlib
import math
import re

import pytest

from . import pgsetup

MASTER_DB_NAME = "master_test"


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
