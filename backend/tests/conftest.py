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
