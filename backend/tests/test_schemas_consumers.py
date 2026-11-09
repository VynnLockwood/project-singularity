"""Task 7: rows, query, duplicates, search and scripts per schema. A table named the same in two schemas never mixes."""
import hashlib
import math
import re

import pytest

from accounts.models import Role, User
from approvals import service
from duplicates import service as dup
from duplicates.models import DuplicateGroup
from schema import introspect, query, rows as rows_mod
from scripts import storage
from scripts.models import Script
from scripts.validate import validate
from search import embedder, indexer
from search.models import DIMENSIONS, SchemaEmbedding

from . import pgsetup

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

NAMES = ["Acme Trading Co., Ltd.", "ACME TRADING COMPANY LIMITED", "Zenith Foods", "Orion Steel"]


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect("singularity_master_rw", master_db) as rw:
        for (s,) in rw.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            rw.execute(f'DROP SCHEMA "{s}" CASCADE')


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def apply(user, ops, schema="master"):
    ch = service.propose(user, ops, source="web", schema=schema)
    assert ch.status == "pending", ch.check_result
    return service.approve(user, ch.pk)


TABLE = {"op": "create_table", "name": "t", "columns": [{"name": "code", "type": "text"}, {"name": "name", "type": "text"}]}


@pytest.fixture
def two(approver):
    """`t` in master (rows M1, M2) and in crm (row C1, plus a duplicate pair)."""
    apply(approver, [{"op": "create_schema", "name": "crm"}])
    for schema in ("master", "crm"):
        apply(approver, [TABLE], schema)
    apply(approver, [{"op": "insert_rows", "table": "t", "rows": [
        {"code": "M1", "name": "Alpha One"}, {"code": "M2", "name": "Beta Two"}]}])
    apply(approver, [{"op": "insert_rows", "table": "t", "rows": [
        {"code": "C1", "name": NAMES[0]}, {"code": "C2", "name": NAMES[1]}, {"code": "C3", "name": NAMES[2]}]}], "crm")
    return approver


def test_rows_and_options_are_per_schema(two):
    m = rows_mod.read_rows("t")
    c = rows_mod.read_rows("t", schema="crm")
    assert {r["code"] for r in m["rows"]} == {"M1", "M2"}
    assert {r["code"] for r in c["rows"]} == {"C1", "C2", "C3"}
    assert {o["label"] for o in rows_mod.options("t", schema="crm")["options"]} == set(NAMES[:3])
    assert {o["label"] for o in rows_mod.options("t")["options"]} == {"Alpha One", "Beta Two"}
    with pytest.raises(introspect.UnknownSchema):
        rows_mod.read_rows("t", schema="public")


def test_rows_table_missing_in_other_schema(two):
    apply(two, [{"op": "create_table", "name": "only_crm", "columns": [{"name": "x", "type": "text"}]}], "crm")
    assert rows_mod.read_rows("only_crm", schema="crm")["total"] == 0
    with pytest.raises(rows_mod.RowsError):
        rows_mod.read_rows("only_crm")


def test_run_select_per_schema_and_refuses_unmanaged(two, monkeypatch):
    assert query.run_select("select code from t order by 1", schema="crm")["rows"] == [["C1"], ["C2"], ["C3"]]
    assert query.run_select("select code from t order by 1")["rows"] == [["M1"], ["M2"]]
    import schema.query as q

    def boom():
        raise AssertionError("SQL ran")
    monkeypatch.setattr(q, "ro_connection", boom)
    for bad in ("public", "singularity", "pg_catalog"):
        with pytest.raises(introspect.UnknownSchema):
            query.run_select("select 1", schema=bad)


class FakeEngine:
    """Stand-in for the Rust engine (not built in every test env): same first word = duplicates."""

    @staticmethod
    def find_duplicates(rows, threshold):
        by = {}
        for pk, vals in rows:
            by.setdefault(vals[0].lower().split()[0], []).append(pk)
        return [(ids, 0.9) for ids in by.values() if len(ids) > 1]


def test_duplicates_scan_and_merge_touch_crm_only(two, monkeypatch):
    monkeypatch.setattr(dup, "_engine", lambda: FakeEngine)
    r = dup.scan("t", two, schema="crm")
    assert r["schema"] == "crm" and r["groups"] == 1
    g = DuplicateGroup.objects.get()
    assert g.schema == "crm" and sorted(g.member_ids) == [1, 2]
    assert dup.scan("t", two)["groups"] == 0  # master.t has no duplicates and does not clear crm groups
    assert DuplicateGroup.objects.filter(schema="crm").count() == 1
    assert dup.group_key("t", [1, 2]) != dup.group_key("t", [1, 2], "crm")
    assert len(dup.detail(g)["members"]) == 2
    ch = dup.propose_merge(g, 1, two)
    assert ch.schema == "crm"
    service.approve(two, ch.pk)
    assert query.run_select("select count(*) from t")["rows"] == [[2]]


def fake_vector(text):
    v = [0.0] * DIMENSIONS
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIMENSIONS] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def test_search_docs_keyed_by_schema(two, settings, monkeypatch):
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [fake_vector(t) for t in texts])
    docs = indexer.build_docs()
    keys = {d["key"] for d in docs}
    assert {"t", "t.code", "crm.t", "crm.t.code"} <= keys
    assert all(d["key"] == d["table"] + ("." + d["column"] if d["column"] else "")
               for d in docs if d["schema"] == "master")
    assert {d["schema"] for d in indexer.build_docs("crm")} == {"crm"}
    indexer.reindex()
    assert SchemaEmbedding.objects.filter(kind="table", table_name="t").count() == 2
    assert indexer.reindex()["embedded"] == 0
    from search import service as search_service
    assert {r["schema"] for r in search_service.search("t")["results"]} <= {"master", "crm"}


SCRIPT = """CREATE TABLE public.province (
    code integer NOT NULL,
    name text
);
COPY public.province (code, name) FROM stdin;
10\tBangkok
20\tPhuket
\\.
"""


def test_script_targets_crm_and_leaves_master_alone(two, settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    s = storage.save(two, "d.sql", SCRIPT.encode())
    r = validate(s, None, "crm")
    assert r["ok"] and r["schema"] == "crm", r
    assert "province" not in {t["name"] for t in introspect.list_tables()}
    assert "province" not in {t["name"] for t in introspect.list_tables("crm")}  # dry run rolled back
    from scripts import service as script_service
    with pytest.raises(storage.ScriptError):
        script_service.submit(two, s.pk, schema="master")
    ch = script_service.submit(two, s.pk)
    assert ch.schema == "crm"
    service.approve(two, ch.pk)
    assert "province" in {t["name"] for t in introspect.list_tables("crm")}
    assert "province" not in {t["name"] for t in introspect.list_tables()}
    assert query.run_select("select count(*) from province", schema="crm")["rows"] == [[2]]


def test_validate_refuses_unmanaged_schema(two, settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    s = storage.save(two, "d.sql", SCRIPT.encode())
    with pytest.raises(introspect.UnknownSchema):
        validate(s, None, "public")


def test_failed_revalidation_for_another_schema_keeps_the_validated_one(two, settings, tmp_path):
    from scripts import service as script_service
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    s = storage.save(two, "d.sql", SCRIPT.encode())
    assert validate(s, None, "master")["ok"]
    apply(two, [{"op": "create_table", "name": "province", "columns": [{"name": "code", "type": "integer"}]}], "crm")
    bad = validate(s, {"tables": {}}, "crm")  # fails: province exists in crm / mapping does not fit
    assert not bad["ok"]
    s.refresh_from_db()
    assert s.status == "validated" and s.report["validated_schema"] == "master"
    with pytest.raises(storage.ScriptError):
        script_service.submit(two, s.pk, schema="crm")
    ch = script_service.submit(two, s.pk)
    assert ch.schema == "master"


def test_reindex_leaves_other_schemas_alone_and_search_keeps_same_names_apart(two, settings, monkeypatch):
    from search import service as search_service
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [fake_vector(t) for t in texts])
    indexer.reindex()
    hits = search_service.search("t", top_k=20)["results"]
    assert {(h["schema"], h["table"]) for h in hits if h["kind"] == "table" and h["table"] == "t"} == {("master", "t"), ("crm", "t")}
    apply(two, [{"op": "drop_table", "table": "t", "reason": "gone"}], "crm")
    out = indexer.reindex()
    assert out["removed"] > 0
    assert not SchemaEmbedding.objects.filter(schema="crm").exists()
    assert SchemaEmbedding.objects.filter(schema="master", table_name="t").count() == 3


@pytest.mark.django_db(transaction=True)
def test_migrations_give_existing_rows_master():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    targets = [("duplicates", "0001_initial"), ("search", "0001_initial")]
    ex = MigrationExecutor(connection)
    ex.migrate(targets)
    try:
        old = ex.loader.project_state(targets).apps
        old.get_model("search", "SchemaEmbedding").objects.create(
            kind="table", table_name="t", content="x", content_hash="h", embedding=[0.0] * DIMENSIONS)
        old.get_model("duplicates", "DuplicateGroup").objects.create(
            table_name="t", key="k", member_ids=[1, 2], score=0.9)
        ex = MigrationExecutor(connection)
        ex.migrate(ex.loader.graph.leaf_nodes())
        assert SchemaEmbedding.objects.get().schema == "master"
        assert DuplicateGroup.objects.get().schema == "master"
    finally:
        ex = MigrationExecutor(connection)
        ex.migrate(ex.loader.graph.leaf_nodes())
