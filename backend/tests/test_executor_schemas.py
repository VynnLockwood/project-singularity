import subprocess

import pytest

from gate import check
from gate.defaults import DEFAULT_RULES
from schema import backup, executor, introspect, query
from schema.conn import rw_connection

from . import pgsetup

pytestmark = pytest.mark.usefixtures("master")

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
ME = "me@example.com"


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect("singularity_master_rw", master_db) as rw:
        for (s,) in rw.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            rw.execute(f'DROP SCHEMA "{s}" CASCADE')


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def apply(ops, schema="master", **kw):
    st = introspect.snapshot(schema)
    managed = {m["name"]: m["tables"] for m in introspect.managed_schemas()}
    res = check.run(ops, st, RULES, actor=ME, schema=schema, managed_schemas=managed, **kw)
    assert res.ok, (res.errors, res.violations)
    return executor.execute(res.operations, st, ME, rules=RULES, schema=schema)


def make_crm():
    return apply([{"op": "create_schema", "name": "crm"}])


def rows(sql):
    r = query.run_select(sql)
    names = [c["name"] if isinstance(c, dict) else c for c in r["columns"]]
    return [dict(zip(names, x)) for x in r["rows"]]


def schema_of(table):
    return [r["table_schema"] for r in rows(
        f"SELECT table_schema FROM information_schema.tables WHERE table_name = '{table}' ORDER BY 1")]


def test_ops_land_in_the_named_schema_only():
    make_crm()
    apply([
        {"op": "create_table", "name": "customer", "columns": [col("code", unique=True), col("name")]},
        {"op": "insert_rows", "table": "customer", "rows": [{"code": "C1", "name": "A"}]},
        {"op": "add_column", "table": "customer", "column": col("city", nullable=True)},
        {"op": "add_index", "table": "customer", "columns": ["name"]},
    ], schema="crm")
    assert schema_of("customer") == ["crm"]
    assert rows("SELECT code FROM crm.customer") == [{"code": "C1"}]
    idx = rows("SELECT schemaname FROM pg_indexes WHERE indexname LIKE '%name%' AND tablename = 'customer'")
    assert {r["schemaname"] for r in idx} == {"crm"}


def test_same_table_name_in_two_schemas_is_independent():
    make_crm()
    t = [{"op": "create_table", "name": "district", "columns": [col("name")]}]
    apply(t)
    apply(t, schema="crm")
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "m"}]}])
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "c1"}, {"name": "c2"}]}], schema="crm")
    assert len(rows("SELECT * FROM master.district")) == 1
    assert len(rows("SELECT * FROM crm.district")) == 2
    apply([{"op": "add_column", "table": "district", "column": col("zone", nullable=True)}], schema="crm")
    assert "zone" not in rows("SELECT * FROM master.district")[0]


def test_foreign_key_inside_schema():
    make_crm()
    apply([
        {"op": "create_table", "name": "province", "columns": [col("code", unique=True)]},
        {"op": "create_table", "name": "district", "columns": [
            col("name"), col("province_id", "integer", references={"table": "province"})]},
        {"op": "insert_rows", "table": "province", "rows": [{"code": "BKK"}]},
    ], schema="crm")
    pid = rows("SELECT province_id FROM crm.province")[0]["province_id"]
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "x", "province_id": pid}]}], schema="crm")
    assert len(rows("SELECT * FROM crm.district")) == 1


def test_create_schema_comment_and_reverse_round_trip():
    res = apply([{"op": "create_schema", "name": "crm", "comment": "customers"}])
    assert res.reverse_ops == [{"op": "drop_schema", "schema": "crm", "reason": "undo create"}]
    assert [s for s in introspect.managed_schemas() if s["name"] == "crm"][0]["comment"] == "customers"
    apply(res.reverse_ops)
    assert [s["name"] for s in introspect.managed_schemas()] == ["master"]


def test_rename_schema_moves_tables_and_reverses():
    make_crm()
    apply([{"op": "create_table", "name": "customer", "columns": [col("name")]}], schema="crm")
    res = apply([{"op": "rename_schema", "schema": "crm", "new_name": "sales"}])
    assert res.reverse_ops == [{"op": "rename_schema", "schema": "sales", "new_name": "crm"}]
    assert schema_of("customer") == ["sales"]
    apply(res.reverse_ops)
    assert schema_of("customer") == ["crm"]


def test_drop_schema_non_empty_fails_with_db_message():
    make_crm()
    apply([{"op": "create_table", "name": "customer", "columns": [col("name")]}], schema="crm")
    st = introspect.snapshot("master")
    with pytest.raises(executor.ApplyError) as e:
        executor.execute([{"op": "drop_schema", "schema": "crm", "reason": "x"}], st, ME, rules=RULES)
    assert str(e.value)
    assert schema_of("customer") == ["crm"]


def test_drop_empty_schema_is_irreversible():
    make_crm()
    res = apply([{"op": "drop_schema", "schema": "crm", "reason": "unused"}])
    assert res.reverse_ops is None
    assert [s["name"] for s in introspect.managed_schemas()] == ["master"]


def seq_last(schema):
    with rw_connection() as conn:
        return [r["last_value"] for r in executor._sequence_states(conn, ["customer"], schema)]


def test_dry_run_in_schema_restores_sequences():
    make_crm()
    apply([{"op": "create_table", "name": "customer", "columns": [col("name")]}], schema="crm")
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"name": "a"}]}], schema="crm")
    before = seq_last("crm")
    st = introspect.snapshot("crm")
    ops = [{"op": "insert_rows", "table": "customer", "rows": [{"name": "b"}, {"name": "c"}]}]
    res = check.run(ops, st, RULES, actor=ME, schema="crm")
    assert res.ok
    out = executor.execute(res.operations, st, ME, rules=RULES, schema="crm", dry_run=True)
    assert out.statements >= 1
    assert seq_last("crm") == before
    assert len(rows("SELECT * FROM crm.customer")) == 1


def test_backup_schema_contains_only_that_schema(tmp_path):
    make_crm()
    apply([{"op": "create_table", "name": "customer", "columns": [col("name")]}], schema="crm")
    apply([{"op": "create_table", "name": "other", "columns": [col("name")]}])
    path = backup.backup_schema("crm", "t")
    assert path.name.startswith("crm-") and path.name.endswith("-t.dump")
    listing = subprocess.run(["pg_restore", "-l", str(path)], capture_output=True, text=True, check=True).stdout
    assert "customer" in listing and "other" not in listing
    assert " master " not in listing


def test_backup_schema_rejects_bad_name():
    for bad in ("Crm", "x;y", "--schema=a", "", "1a"):
        with pytest.raises(backup.BackupError):
            backup.backup_schema(bad, "t")


def test_prune_handles_schema_dumps_and_ignores_other_files(tmp_path):
    import os
    folder = tmp_path / "b"
    folder.mkdir()
    old = folder / "crm-20200101T000000Z-x.dump"
    stray = folder / "notes-old.dump"
    for f in (old, stray):
        f.write_text("x")
        os.utime(f, (0, 0))
    assert backup.prune(folder, 14) == 1
    assert not old.exists() and stray.exists()


def test_backup_rejects_bad_label():
    for bad in ("../x", "a/b", "", "a b", "x..y"):
        with pytest.raises(backup.BackupError):
            backup.backup_schema("master", bad)


def test_helpers_require_schema():
    with pytest.raises(TypeError):
        executor._insert_targets([], {})
