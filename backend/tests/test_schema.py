import os
import subprocess

import pytest

from gate import check
from gate.defaults import DEFAULT_RULES
from schema import backup, executor, introspect, query

pytestmark = pytest.mark.usefixtures("master")

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
ME = "me@example.com"


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def apply(ops, *, skip_rules=False, max_rows=1000):
    st = introspect.snapshot()
    res = check.run(ops, st, RULES, actor=ME, skip_rules=skip_rules)
    assert res.ok, (res.errors, res.violations)
    return executor.execute(res.operations, st, ME, max_rows=max_rows, rules=RULES)


def make_customer(extra=None):
    cols = [col("customer_code", unique=True), col("name", comment="Legal name"), *(extra or [])]
    return apply([{"op": "create_table", "name": "customer", "columns": cols, "comment": "Customers"}])


def rows(sql):
    return query.run_select(sql)["rows"]


# ---- create + introspect ---------------------------------------------------

def test_create_table_and_introspect():
    res = make_customer()
    assert res.reverse_ops == [{"op": "drop_table", "table": "customer", "reason": "revert"}]

    assert [t["name"] for t in introspect.list_tables()] == ["customer"]
    d = introspect.describe_table("customer")
    names = [c["name"] for c in d["columns"]]
    assert names == ["customer_id", "created_at", "updated_at", "created_by", "is_active", "customer_code", "name"]
    assert d["comment"] == "Customers"
    by = {c["name"]: c for c in d["columns"]}
    assert by["customer_id"]["identity"] and by["customer_id"]["type"] == "integer"
    assert by["name"]["comment"] == "Legal name"
    assert by["is_active"]["default"] == "true" and by["is_active"]["nullable"] is False
    kinds = {c["kind"] for c in d["constraints"]}
    assert {"primary_key", "unique"} <= kinds


def test_describe_missing_table_is_none():
    assert introspect.describe_table("nope") is None


def test_snapshot_matches_what_the_gate_expects():
    make_customer()
    snap = introspect.snapshot()
    assert snap["customer"]["columns"]["customer_id"] == {"type": "integer", "nullable": False, "default": None}
    assert "customer_pkey" in snap["customer"]["constraints"]
    # A follow-up op validated against the real snapshot works
    res = check.run([{"op": "add_column", "table": "customer", "column": col("email")}], snap, RULES)
    assert res.ok


def test_foreign_key_roundtrip():
    make_customer()
    apply([{
        "op": "create_table", "name": "invoice",
        "columns": [col("invoice_code", unique=True), col("customer_id", "integer", references={"table": "customer"})],
    }])
    d = introspect.describe_table("invoice")
    assert any(c["kind"] == "foreign_key" and "REFERENCES master.customer" in c["definition"] for c in d["constraints"])


# ---- data ops and reverse capture -----------------------------------------

def test_insert_autofills_created_by_and_reverses_to_deactivate():
    make_customer()
    res = apply([{"op": "insert_rows", "table": "customer", "rows": [
        {"customer_code": "C1", "name": "Acme"}, {"customer_code": "C2", "name": "Beta"}]}])
    assert rows('SELECT "customer_code", "created_by", "is_active" FROM customer ORDER BY customer_id') == [
        ["C1", ME, True], ["C2", ME, True]]
    assert res.reverse_ops == [{"op": "deactivate_rows", "table": "customer", "where": {"customer_id": [1, 2]}}]


def test_update_sets_updated_at_and_reverse_restores_old_values():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [
        {"customer_code": "C1", "name": "Acme"}, {"customer_code": "C2", "name": "Acme"}]}])
    before = rows('SELECT "updated_at" FROM customer WHERE customer_id = 1')[0][0]
    res = apply([{"op": "update_rows", "table": "customer", "where": {"customer_code": ["C1", "C2"]},
                  "set": {"name": "Acme Ltd"}}])
    assert {r[0] for r in rows("SELECT name FROM customer")} == {"Acme Ltd"}
    assert rows('SELECT "updated_at" FROM customer WHERE customer_id = 1')[0][0] >= before
    # both rows shared the same old value -> one grouped restore op
    assert res.reverse_ops == [{"op": "update_rows", "table": "customer", "where": {"customer_id": [1, 2]}, "set": {"name": "Acme"}}]
    apply(res.reverse_ops, skip_rules=True)
    assert {r[0] for r in rows("SELECT name FROM customer")} == {"Acme"}


def test_deactivate_and_reverse():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "A"}]}])
    res = apply([{"op": "deactivate_rows", "table": "customer", "where": {"customer_code": "C1"}}])
    assert rows('SELECT "is_active" FROM customer') == [[False]]
    apply(res.reverse_ops, skip_rules=True)
    assert rows('SELECT "is_active" FROM customer') == [[True]]


def test_jsonb_and_quotes_roundtrip():
    apply([{"op": "create_table", "name": "doc", "columns": [col("doc_code", unique=True), col("body", "jsonb"), col("title")]}])
    apply([{"op": "insert_rows", "table": "doc", "rows": [
        {"doc_code": "D1", "body": {"a": [1, 2]}, "title": "O'Brien \"x\" %s"}]}])
    assert rows('SELECT body, title FROM doc') == [[{"a": [1, 2]}, "O'Brien \"x\" %s"]]


def test_row_limit_rolls_back_everything():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [
        {"customer_code": f"C{i}", "name": "n"} for i in range(5)]}])
    with pytest.raises(executor.ApplyError, match="more than 3 rows"):
        apply([{"op": "update_rows", "table": "customer", "where": {"name": "n"}, "set": {"name": "x"}}], max_rows=3)
    assert {r[0] for r in rows("SELECT name FROM customer")} == {"n"}


def test_row_limit_also_applies_to_tables_without_an_id():
    # The rules normally force an id column; with that rule off, only the post-update check can enforce the cap.
    global RULES
    saved = RULES
    RULES = [r for r in RULES if r["name"] != "auditColumns"]
    try:
        apply([{"op": "create_table", "name": "tag", "columns": [col("label"), col("kind")]}])
        apply([{"op": "insert_rows", "table": "tag", "rows": [{"label": f"l{i}", "kind": "k"} for i in range(5)]}])
        with pytest.raises(executor.ApplyError, match="touched 5 rows"):
            apply([{"op": "update_rows", "table": "tag", "where": {"kind": "k"}, "set": {"kind": "z"}}], max_rows=3)
        assert {r[0] for r in rows("SELECT kind FROM tag")} == {"k"}          # rolled back
        res = apply([{"op": "update_rows", "table": "tag", "where": {"kind": "k"}, "set": {"kind": "z"}}])
        assert res.reverse_ops is None                                          # no id -> cannot be undone
    finally:
        RULES = saved


# ---- schema ops and reverse capture ---------------------------------------

def test_add_column_rename_comment_type_reverses():
    make_customer([col("score")])
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "A", "score": "7"}]}])

    r = apply([{"op": "add_column", "table": "customer", "column": col("phone_number", comment="Main phone")}])
    assert r.reverse_ops == [{"op": "drop_column", "table": "customer", "column": "phone_number", "reason": "revert"}]

    r = apply([{"op": "rename_column", "table": "customer", "column": "phone_number", "new_name": "phone"}])
    assert r.reverse_ops == [{"op": "rename_column", "table": "customer", "column": "phone", "new_name": "phone_number"}]

    r = apply([{"op": "set_column_comment", "table": "customer", "column": "phone", "comment": "New"}])
    assert r.reverse_ops[0]["comment"] == "Main phone"

    r = apply([{"op": "alter_column_type", "table": "customer", "column": "score", "type": "integer"}])
    assert r.reverse_ops == [{"op": "alter_column_type", "table": "customer", "column": "score", "type": "text"}]
    assert rows("SELECT score FROM customer") == [[7]]
    apply(r.reverse_ops)
    assert rows("SELECT pg_typeof(score)::text FROM customer") == [["text"]]


def test_rename_table_index_unique_fk_reverses():
    make_customer()
    apply([{"op": "create_table", "name": "invoice", "columns": [col("invoice_code", unique=True), col("customer_id", "integer")]}])
    r = apply([{"op": "add_index", "table": "invoice", "columns": ["customer_id"]}])
    assert r.reverse_ops == [{"op": "drop_index", "name": "invoice_customer_id_idx", "reason": "revert"}]
    apply(r.reverse_ops, skip_rules=True)
    r = apply([{"op": "add_foreign_key", "table": "invoice", "column": "customer_id", "ref_table": "customer"}])
    assert r.reverse_ops == [{"op": "drop_constraint", "table": "invoice", "name": "fk_invoice_customer_id", "reason": "revert"}]
    apply(r.reverse_ops, skip_rules=True)
    r = apply([{"op": "add_unique", "table": "invoice", "columns": ["customer_id"]}])
    assert r.reverse_ops[0]["name"] == "uq_invoice_customer_id"
    r = apply([{"op": "rename_table", "table": "invoice", "new_name": "bill"}])
    assert r.reverse_ops == [{"op": "rename_table", "table": "bill", "new_name": "invoice"}]


def test_drops_are_irreversible():
    make_customer([col("tmp")])
    r = apply([{"op": "drop_column", "table": "customer", "column": "tmp", "reason": "unused"}])
    assert r.reverse_ops is None
    r = apply([{"op": "drop_table", "table": "customer", "reason": "test"}])
    assert r.reverse_ops is None
    assert introspect.list_tables() == []


def test_multi_op_change_reverses_in_opposite_order():
    r = apply([
        {"op": "create_table", "name": "a", "columns": [col("a_code", unique=True)]},
        {"op": "add_column", "table": "a", "column": col("note")},
    ])
    assert [o["op"] for o in r.reverse_ops] == ["drop_column", "drop_table"]
    apply(r.reverse_ops, skip_rules=True)
    assert introspect.list_tables() == []


def test_failure_rolls_back_the_whole_change():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [
        {"customer_code": "C1", "name": "same"}, {"customer_code": "C2", "name": "same"}]}])
    with pytest.raises(executor.ApplyError, match="could not create unique index"):
        apply([
            {"op": "create_table", "name": "other", "columns": [col("o_code", unique=True)]},
            {"op": "add_unique", "table": "customer", "columns": ["name"]},   # duplicates -> fails
        ])
    assert [t["name"] for t in introspect.list_tables()] == ["customer"]   # 'other' was rolled back


def test_ro_role_cannot_write_even_by_mistake():
    import psycopg
    from schema.conn import ro_connection

    make_customer()
    with ro_connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE master.sneaky (id int)")


# ---- fingerprint -----------------------------------------------------------

def test_fingerprint_changes_when_structure_changes():
    f_absent = introspect.fingerprint(["customer"])
    make_customer()
    f1 = introspect.fingerprint(["customer"])
    assert f1 != f_absent
    assert introspect.fingerprint(["customer"]) == f1               # stable
    apply([{"op": "add_column", "table": "customer", "column": col("email")}])
    assert introspect.fingerprint(["customer"]) != f1
    assert introspect.fingerprint(["unrelated"]) == introspect.fingerprint(["unrelated"])


def test_fingerprint_ignores_row_changes():
    make_customer()
    f = introspect.fingerprint(["customer"])
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "A"}]}])
    assert introspect.fingerprint(["customer"]) == f


# ---- backup ----------------------------------------------------------------

def test_backup_writes_a_restorable_dump(settings):
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "A"}]}])
    path = backup.backup_master("change-7")
    assert path.exists() and path.name.endswith("-change-7.dump") and path.stat().st_size > 0
    listing = subprocess.run(["pg_restore", "-l", str(path)], capture_output=True, text=True, check=True).stdout
    assert "customer" in listing


def test_backup_prunes_old_dumps(settings, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    old = folder / "master-20200101T000000Z-x.dump"
    old.write_text("old")
    os.utime(old, (0, 0))
    keep = folder / "master-recent.dump"
    keep.write_text("new")
    assert backup.prune(folder, 14) == 1
    assert not old.exists() and keep.exists()


def test_backup_failure_is_reported(settings):
    settings.MASTER_DB = {**settings.MASTER_DB, "dbname": "does_not_exist"}
    with pytest.raises(backup.BackupError):
        backup.backup_master("x")


# ---- read-only query -------------------------------------------------------

def test_query_returns_rows_and_columns():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "A"}]}])
    out = query.run_select('SELECT "customer_code", name FROM customer')
    assert out == {"columns": ["customer_code", "name"], "rows": [["C1", "A"]], "truncated": False}


def test_query_truncates():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": f"C{i}", "name": "n"} for i in range(5)]}])
    out = query.run_select("SELECT customer_id FROM customer", limit=3)
    assert len(out["rows"]) == 3 and out["truncated"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "DELETE FROM customer",
        "UPDATE customer SET name = 'x'",
        "DROP TABLE customer",
        "SELECT 1; DROP TABLE customer",
        "  insert into customer (name) values ('x')",
        "",
        None,
    ],
)
def test_query_rejects_non_select(bad):
    make_customer()
    with pytest.raises(query.QueryError):
        query.run_select(bad)
    assert [t["name"] for t in introspect.list_tables()] == ["customer"]


def test_query_cannot_write_through_a_cte():
    make_customer()
    apply([{"op": "insert_rows", "table": "customer", "rows": [{"customer_code": "C1", "name": "A"}]}])
    with pytest.raises(query.QueryError):
        query.run_select("WITH d AS (DELETE FROM customer RETURNING customer_id) SELECT * FROM d")
    assert rows("SELECT count(*) FROM customer") == [[1]]


def test_query_timeout():
    with pytest.raises(query.QueryError, match="timeout"):
        query.run_select("SELECT pg_sleep(2)", timeout_ms=100)


def test_query_cannot_see_singularity_schema():
    with pytest.raises(query.QueryError):
        query.run_select("SELECT * FROM singularity.settings_app_rule")
