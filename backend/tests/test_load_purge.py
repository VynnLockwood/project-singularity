import os
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import Role
from scripts import loads, storage
from scripts.models import StagedLoad
from tests.mcp_helpers import applied, make_users

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    from config.siconfig import invalidate_overrides
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    invalidate_overrides()
    yield
    invalidate_overrides()


@pytest.fixture
def ed():
    u = make_users()
    applied(u[Role.APPROVER], [{"op": "create_table", "name": "customer", "columns": [{"name": "name_en", "type": "text"}]}])
    return u[Role.EDITOR]


def path(lid):
    return os.path.join(storage.loads_dir(), StagedLoad.objects.get(pk=lid).path)


def test_open_expired_load_is_expired_and_file_deleted(ed):
    lid = loads.begin(ed, "customer", ["name_en"])["load_id"]
    p = path(lid)
    StagedLoad.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert loads.purge_expired() == 1
    assert StagedLoad.objects.get(pk=lid).status == "expired" and not os.path.exists(p)
    assert loads.purge_expired() == 0


def test_open_fresh_load_is_kept(ed):
    lid = loads.begin(ed, "customer", ["name_en"])["load_id"]
    assert loads.purge_expired() == 0
    assert os.path.exists(path(lid))


def test_orphan_file_of_cancelled_load_is_removed(ed):
    lid = loads.begin(ed, "customer", ["name_en"])["load_id"]
    p = path(lid)
    StagedLoad.objects.filter(pk=lid).update(status="cancelled")   # as if the file removal had failed
    assert loads.purge_expired() == 1
    assert not os.path.exists(p) and StagedLoad.objects.get(pk=lid).status == "cancelled"


def test_append_after_expiry_refuses(ed):
    lid = loads.begin(ed, "customer", ["name_en"])["load_id"]
    StagedLoad.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    loads.purge_expired()
    with pytest.raises(loads.LoadError, match="expired"):
        loads.append(ed, lid, 1, "a\n")
