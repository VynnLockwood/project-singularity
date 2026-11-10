"""upsert_rows (Plan B, task 6)."""
import pytest

from gate import operations, sqlgen
from schema import executor

from .ops_helpers import ME, apply, col, gate, make_table, rows

pytestmark = pytest.mark.usefixtures("master")

UP = {"op": "upsert_rows", "table": "t", "key": ["code"], "columns": ["name"],
      "rows": [{"code": "A", "name": "Alpha"}, {"code": "B", "name": "Beta"}]}


def table(rows_=None, audit=False):
    extra = [col("code", unique=True), col("name")]
    if audit:
        extra += [col("created_by"), col("updated_at", "timestamptz", default="now()")]
    make_table("t", extra=extra, rows=rows_)


def errors_of(op):
    try:
        operations.parse_ops([op])
    except operations.OpError as e:
        return e.errors
    return []


def test_shape():
    assert operations.parse_ops([UP]) and "upsert_rows" in operations.DATA_OPS
    for bad in ({**UP, "key": []}, {**UP, "columns": []}, {**UP, "columns": ["code"]}, {**UP, "rows": []},
                {**UP, "rows": [{"name": "x"}]}, {**UP, "rows": [{"code": "A"}]}, {**UP, "x": 1},
                {**UP, "columns": ["name", "other"]}):
        assert errors_of(bad), bad


def test_key_must_match_a_unique_constraint_or_primary_key():
    table()
    assert gate([UP]).ok
    bad = gate([{**UP, "key": ["name"], "columns": ["code"], "rows": [{"name": "x", "code": "y"}]}])
    assert any("name" in e and "unique" in e for e in bad.errors)
    assert gate([{**UP, "key": ["t_id"], "columns": ["name"], "rows": [{"t_id": 1, "name": "x"}]}]).ok
    apply([{"op": "add_unique", "table": "t", "columns": ["code", "name"]}])
    assert gate([{**UP, "key": ["name", "code"], "columns": [], "rows": []}]).errors     # shape: non-empty columns
    assert gate([{"op": "upsert_rows", "table": "t", "key": ["code", "name"], "columns": ["t_id"],
                  "rows": [{"code": "A", "name": "x", "t_id": 5}]}]).ok
    assert gate([{**UP, "rows": [{"code": "A", "name": "x", "nope": 1}]}]).errors


def test_upsert_sql_and_diff():
    table(audit=True)
    res = gate([UP])
    assert res.ok, res.errors
    assert res.sql[0] == ('INSERT INTO "master"."t" ("code", "name", "created_by") VALUES (\'A\', \'Alpha\', \'' + ME + '\') '
                          'ON CONFLICT ("code") DO UPDATE SET "name" = EXCLUDED."name", "updated_at" = now();')
    assert res.diff == ["~ upsert 2 row(s) into t on (code)"]


def test_upsert_inserts_and_updates():
    table([{"code": "A", "name": "old", "created_by": "orig"}], audit=True)
    apply([UP])
    # the updated row keeps its creator; the inserted one is stamped with the actor
    assert rows("SELECT code, name, created_by FROM t ORDER BY code") == [["A", "Alpha", "orig"], ["B", "Beta", ME]]


def test_upsert_reverse_restores_updated_values_and_deletes_inserted_rows():
    table([{"code": "A", "name": "old"}])
    before = rows("SELECT code, name FROM t ORDER BY code")
    res = apply([UP])
    kinds = sorted(o["op"] for o in res.reverse_ops)
    assert kinds == ["delete_rows", "update_rows"]
    apply(res.reverse_ops)
    assert rows("SELECT code, name FROM t ORDER BY code") == before


def test_composite_key_reverse_deletes_row_by_row():
    table()
    apply([{"op": "add_unique", "table": "t", "columns": ["code", "name"]}])
    op = {"op": "upsert_rows", "table": "t", "key": ["code", "name"], "columns": ["t_id"],
          "rows": [{"code": "A", "name": "x", "t_id": 10}, {"code": "B", "name": "y", "t_id": 11}]}
    res = apply([op])
    assert [o["op"] for o in res.reverse_ops] == ["delete_rows", "delete_rows"]
    apply(res.reverse_ops)
    assert rows("SELECT count(*) FROM t") == [[0]]


def test_upsert_over_the_cap_is_irreversible():
    table()
    res = apply([UP], max_rows=1)
    assert res.reverse_ops is None and rows("SELECT count(*) FROM t") == [[2]]


def test_ill_typed_value_fails_cleanly():
    table()
    with pytest.raises(executor.ApplyError):
        apply([{"op": "upsert_rows", "table": "t", "key": ["code"], "columns": ["t_id"],
                "rows": [{"code": "A", "t_id": "not a number"}]}])
