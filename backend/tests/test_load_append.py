import os

import pytest

from accounts.models import Role
from scripts import loads, storage
from scripts.loadcsv import to_copy_line
from scripts.models import StagedLoad
from settings_app import tunables
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
def ctx():
    u = make_users()
    applied(u[Role.APPROVER], [{"op": "create_table", "name": "customer", "columns": [
        {"name": "name_en", "type": "text", "nullable": False}, {"name": "age", "type": "integer"}]}])
    ed = u[Role.EDITOR]
    lid = loads.begin(ed, "customer", ["name_en", "age"])["load_id"]
    return ed, u[Role.APPROVER], lid


def content(lid):
    return open(os.path.join(storage.loads_dir(), StagedLoad.objects.get(pk=lid).path), "rb").read().decode()


def test_three_chunks_in_order(ctx):
    ed, _, lid = ctx
    assert loads.append(ed, lid, 1, "a,1\nb,2\n") == {"ok": True, "stored": True, "rows_in_chunk": 2,
                                                       "total_rows": 2, "next_seq": 2}
    loads.append(ed, lid, 2, "c,3\n")
    out = loads.append(ed, lid, 3, "d,\n")
    assert out["total_rows"] == 4 and out["next_seq"] == 4
    assert content(lid) == "a\t1\nb\t2\nc\t3\nd\t\\N\n"


def test_retry_same_seq_same_content_is_noop(ctx):
    ed, _, lid = ctx
    loads.append(ed, lid, 1, "a,1\n")
    size = len(content(lid))
    out = loads.append(ed, lid, 1, "a,1\n")
    assert out["duplicate"] and out["next_seq"] == 2 and out["total_rows"] == 1
    assert len(content(lid)) == size


def test_same_seq_different_content_errors(ctx):
    ed, _, lid = ctx
    loads.append(ed, lid, 1, "a,1\n")
    with pytest.raises(loads.LoadError, match="different content.*expected seq 2"):
        loads.append(ed, lid, 1, "z,9\n")


@pytest.mark.parametrize("seq", [2, 0, 5])
def test_wrong_seq_says_expected(ctx, seq):
    ed, _, lid = ctx
    with pytest.raises(loads.LoadError, match="expected seq 1"):
        loads.append(ed, lid, seq, "a,1\n")


def test_bad_chunk_stores_nothing_and_numbers_rows_across_chunks(ctx):
    ed, _, lid = ctx
    loads.append(ed, lid, 1, "a,1\nb,2\n")
    out = loads.append(ed, lid, 2, "c,3\nd,notanumber\n")
    assert out["ok"] is False and out["stored"] is False
    assert out["problems"][0]["row"] == 4 and out["problems"][0]["column"] == "age"
    st = StagedLoad.objects.get(pk=lid)
    assert (st.next_seq, st.rows) == (2, 2) and content(lid) == "a\t1\nb\t2\n"
    assert loads.append(ed, lid, 2, "c,3\nd,4\n")["total_rows"] == 4   # the same seq can be resent fixed


def test_malformed_csv_is_a_problem_with_row(ctx):
    ed, _, lid = ctx
    loads.append(ed, lid, 1, "a,1\n")
    out = loads.append(ed, lid, 2, 'b,2\n"c,3\n')
    assert out["problems"][0]["row"] == 3 and not out["stored"]


def test_file_longer_than_bytes_is_cut_back(ctx):
    ed, _, lid = ctx
    loads.append(ed, lid, 1, "a,1\n")
    with open(os.path.join(storage.loads_dir(), StagedLoad.objects.get(pk=lid).path), "ab") as f:
        f.write(b"GARBAGE\n")
    loads.append(ed, lid, 2, "b,2\n")
    assert content(lid) == "a\t1\nb\t2\n"


def test_crash_between_write_and_commit(ctx, monkeypatch):
    ed, _, lid = ctx
    real = StagedLoad.save
    calls = []

    def boom(self, *a, **k):
        if not calls:
            calls.append(1)
            raise RuntimeError("crash")
        return real(self, *a, **k)
    monkeypatch.setattr(StagedLoad, "save", boom)
    with pytest.raises(RuntimeError):
        loads.append(ed, lid, 1, "a,1\n")
    assert StagedLoad.objects.get(pk=lid).next_seq == 1
    assert loads.append(ed, lid, 1, "a,1\n")["total_rows"] == 1
    assert content(lid) == "a\t1\n"


def test_limits(ctx):
    ed, appr, lid = ctx
    tunables.apply(appr, {"limits.maxLoadChunkRows": 2})
    with pytest.raises(loads.LoadError, match="3 rows"):
        loads.append(ed, lid, 1, "a,1\nb,2\nc,3\n")
    tunables.apply(appr, {"limits.maxLoadChunkBytes": 4096})
    with pytest.raises(loads.LoadError, match="bytes"):
        loads.append(ed, lid, 1, "a," + "1" * 5000 + "\n")
    tunables.apply(appr, {"limits.maxScriptRows": 3})
    loads.append(ed, lid, 1, "a,1\nb,2\n")
    with pytest.raises(loads.LoadError, match="3 rows in total"):
        loads.append(ed, lid, 2, "c,3\nd,4\n")
    tunables.apply(appr, {"limits.maxLoadBytes": 1048576})
    StagedLoad.objects.filter(pk=lid).update(bytes=1048570)   # pretend nearly full
    with pytest.raises(loads.LoadError):
        loads.append(ed, lid, 2, "c,3\n")


def test_empty_chunk_refused(ctx):
    ed, _, lid = ctx
    with pytest.raises(loads.LoadError, match="empty"):
        loads.append(ed, lid, 1, "\n")


def test_closed_and_expired_loads_refuse(ctx):
    ed, _, lid = ctx
    from django.utils import timezone
    StagedLoad.objects.filter(pk=lid).update(expires_at=timezone.now())
    with pytest.raises(loads.LoadError, match="expired"):
        loads.append(ed, lid, 1, "a,1\n")
    loads.cancel(ed, lid)
    with pytest.raises(loads.LoadError, match="cancelled"):
        loads.append(ed, lid, 1, "a,1\n")


def test_expiry_is_extended_by_append(ctx):
    ed, _, lid = ctx
    from datetime import timedelta
    from django.utils import timezone
    StagedLoad.objects.filter(pk=lid).update(expires_at=timezone.now() + timedelta(minutes=1))
    loads.append(ed, lid, 1, "a,1\n")
    assert StagedLoad.objects.get(pk=lid).expires_at > timezone.now() + timedelta(hours=23)


def test_special_characters_are_stored_escaped(ctx):
    ed, _, lid = ctx
    rows = [["a\tb", "1"], ["c\\d\ne", "2"], ["\\N", None], ["é", "3"]]
    csv = ('"a\tb",1\n"c\\d\ne",2\n"\\N",\né,3\n')
    loads.append(ed, lid, 1, csv)
    assert content(lid) == "".join(to_copy_line(r) + "\n" for r in rows)


def test_other_users_load_is_missing(ctx):
    _, _, lid = ctx
    from accounts.models import User
    other = User.objects.create(username="e2@x.io", email="e2@x.io", role=Role.EDITOR)
    with pytest.raises(loads.LoadError, match="does not exist"):
        loads.append(other, lid, 1, "a,1\n")
