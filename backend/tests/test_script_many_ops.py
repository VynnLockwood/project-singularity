"""A dump with more than maxOpsPerProposal operations (30 tables = 60 ops) must validate and submit."""
import pytest

from accounts.models import Role, User
from scripts import service, storage
from tests import bigdump

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    from config.siconfig import invalidate_overrides
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    invalidate_overrides()
    yield
    invalidate_overrides()


def test_sixty_operations_validate_and_submit(tmp_path):
    ed = User.objects.create(username="ed@x.io", email="ed@x.io", role=Role.EDITOR)
    bigdump.write_dump(tmp_path / "d.sql", tables=30, rows=60, target_bytes=20_000)
    script = storage.save(ed, "d.sql", (tmp_path / "d.sql").read_bytes())
    report = service.validate(ed, script.pk, None)
    assert report["ok"], (report["errors"][:2], report["gate"])
    assert report["ops_summary"]["total"] == 60
    change = service.submit(ed, script.pk)
    assert change.status == "pending" and len(change.operations) == 60
