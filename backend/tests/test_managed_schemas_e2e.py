"""Managed schemas end to end, through the real approval path and the real restricted roles."""
import shutil
import subprocess

import pytest

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from schema import conn, introspect, query
from schema.executor import ApplyError
from schema.export import open_export

from . import pgsetup

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

DST = "managed_e2e_dst"


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
    return User.objects.create(username="appr", email="appr@example.com", role=Role.APPROVER)


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def run(approver, ops, schema="master"):
    c = service.propose(approver, ops, source="web", schema=schema)
    assert c.status == "pending", (c.status, c.check_result)
    c = service.approve(approver, c.pk)
    assert c.status == "applied", c.error
    return c


def master_state():
    counts = {}
    for t in introspect.list_tables("master"):
        counts[t["name"]] = query.run_select(f'SELECT count(*) AS n FROM master."{t["name"]}"')["rows"][0][0]
    names = sorted(counts)
    return counts, introspect.fingerprint(names, "master")


def crm_snapshot(dbname, schema):
    with pgsetup.connect(pgsetup.ADMIN, dbname) as c:
        out = {
            "constraints": [tuple(r) for r in c.execute(
                "SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid) FROM pg_constraint k"
                " JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = %s ORDER BY 1, 2", (schema,)).fetchall()],
            "tables": [r[0] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY 1", (schema,)).fetchall()],
        }
        out["rows"] = {t: tuple(c.execute(
            f'SELECT count(*), md5(coalesce(string_agg(t::text, E\'\\n\' ORDER BY t::text), \'\')) FROM "{schema}"."{t}" t'
        ).fetchone()) for t in out["tables"]}
    return out


def test_managed_schema_lifecycle(approver, tmp_path):
    # master content, created through the same path
    run(approver, [{"op": "create_table", "name": "district", "columns": [col("name")]}])
    run(approver, [{"op": "insert_rows", "table": "district", "rows": [{"name": "North"}, {"name": "South"}]}])
    before = master_state()
    assert before[0] == {"district": 2}

    # create schema, then tables with an FK and rows
    run(approver, [{"op": "create_schema", "name": "crm"}])
    assert "crm" in [s["name"] for s in introspect.managed_schemas()]
    run(approver, [{"op": "create_table", "name": "company", "columns": [col("name")]}], "crm")
    mk = run(approver, [{"op": "create_table", "name": "contact", "columns": [
        col("name"), col("company_id", "integer", references={"table": "company"})]}], "crm")
    run(approver, [{"op": "insert_rows", "table": "company", "rows": [{"name": "Acme"}, {"name": "Beta"}]}], "crm")
    run(approver, [{"op": "insert_rows", "table": "contact", "rows": [
        {"name": "Ann", "company_id": 1}, {"name": "Bob", "company_id": 2}]}], "crm")

    res = query.run_select(
        "SELECT c.name AS contact, o.name AS company FROM contact c JOIN company o ON o.company_id = c.company_id ORDER BY c.contact_id",
        schema="crm")
    assert [tuple(r) for r in res["rows"]] == [("Ann", "Acme"), ("Bob", "Beta")]
    assert master_state() == before

    # a pending crm change goes stale after the rename; export first
    pending = service.propose(approver, [{"op": "create_table", "name": "note", "columns": [col("body")]}],
                              source="web", schema="crm")
    assert pending.status == "pending"

    if shutil.which("psql") is not None:
        path = tmp_path / "crm.sql"
        exp = open_export("crm", data=True, cap=10_000)
        try:
            with open(path, "wb") as f:
                for chunk in exp.chunks():
                    f.write(chunk)
        finally:
            exp.close()
        with pgsetup.connect(pgsetup.ADMIN, "postgres") as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {DST} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {DST}")
        try:
            r = subprocess.run(
                ["psql", "-v", "ON_ERROR_STOP=1", "-q", "-h", pgsetup.HOST, "-p", str(pgsetup.PORT),
                 "-U", pgsetup.ADMIN, "-d", DST, "-f", str(path)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            src = crm_snapshot("master_test", "crm")
            assert src["tables"] == ["company", "contact"] and src["rows"]["contact"][0] == 2
            assert crm_snapshot(DST, "crm") == src
        finally:
            pgsetup.drop_db(DST)

    # rename crm -> sales
    run(approver, [{"op": "rename_schema", "schema": "crm", "new_name": "sales"}])
    assert [t["name"] for t in introspect.list_tables("sales")] == ["company", "contact"]
    with pytest.raises(introspect.UnknownSchema):
        introspect.list_tables("crm")
    assert query.run_select("SELECT count(*) AS n FROM contact", schema="sales")["rows"][0][0] == 2
    stale = service.approve(approver, pending.pk)
    assert stale.status == "stale" and "crm" in stale.error
    assert master_state() == before

    # non-empty drop is refused (also when a caller reaches the DB function directly as rw)
    c = service.propose(approver, [{"op": "drop_schema", "schema": "sales", "reason": "cleanup"}], source="web")
    if c.status == "pending":
        c = service.approve(approver, c.pk)
    assert c.status != "applied"
    assert introspect.list_tables("sales")
    with conn.rw_connection(autocommit=True) as rw:
        with pytest.raises(Exception, match="still contains objects"):
            rw.execute("SELECT singularity.drop_managed_schema('sales')")

    # drop the tables (child first), then the schema
    run(approver, [{"op": "drop_table", "table": "contact", "reason": "cleanup"}], "sales")
    run(approver, [{"op": "drop_table", "table": "company", "reason": "cleanup"}], "sales")
    run(approver, [{"op": "drop_schema", "schema": "sales", "reason": "cleanup"}])
    assert "sales" not in [s["name"] for s in introspect.managed_schemas()]
    assert master_state() == before


def test_revert_of_create_table_in_managed_schema(approver):
    run(approver, [{"op": "create_schema", "name": "crm"}])
    c = run(approver, [{"op": "create_table", "name": "company", "columns": [col("name")]}], "crm")
    assert [t["name"] for t in introspect.list_tables("crm")] == ["company"]
    rev = service.revert(approver, c.pk)
    assert rev.status == "applied", rev.error
    assert rev.schema == "crm"
    assert introspect.list_tables("crm") == []


def test_schema_ops_fail_readably_without_the_migration(approver, settings):
    """Before the owner runs the migration the functions are missing: create_schema ends failed, with a message."""
    name = "managed_e2e_nofn"
    pgsetup.create_scratch_db(name)
    try:
        with pgsetup.connect(pgsetup.ADMIN, name) as admin:
            for fn in ("create_managed_schema(text)", "rename_managed_schema(text, text)", "drop_managed_schema(text)"):
                admin.execute(f"DROP FUNCTION singularity.{fn}")
        settings.MASTER_DB = {**settings.MASTER_DB, "dbname": name}
        c = service.propose(approver, [{"op": "create_schema", "name": "crm"}], source="web")
        assert c.status == "pending"
        c = service.approve(approver, c.pk)
        assert c.status == "failed"
        assert "create_managed_schema" in c.error and "does not exist" in c.error
        assert [s["name"] for s in introspect.managed_schemas()] == ["master"]
    finally:
        pgsetup.drop_db(name)
