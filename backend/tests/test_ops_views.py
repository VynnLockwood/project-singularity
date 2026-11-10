"""create_view / drop_view (Plan B, task 3)."""
import pytest

from gate import operations, sqlgen
from gate.state import State, validate, views_of
from schema import executor, introspect, query
from schema.conn import rw_connection

from .ops_helpers import RULES, apply, col, gate, make_table, rows

pytestmark = pytest.mark.usefixtures("master")

V = {"op": "create_view", "name": "v", "query": "SELECT a FROM t"}


@pytest.mark.parametrize("q", ["SELECT a FROM t", "WITH x AS (SELECT 1 AS n) SELECT * FROM x",
                               "SELECT a FROM t UNION SELECT a FROM u", "select 1"])
def test_view_query_accepted(q):
    assert operations.check_view_query(q) == []


@pytest.mark.parametrize("q", ["SELECT 1; SELECT 2", "SELECT a INTO x FROM t", "SELECT a FROM t FOR UPDATE",
                               "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "DELETE FROM t",
                               "", "   ", "SELECT 1 /*" + "x" * 11000 + "*/", "SELEC a FROM t",
                               "SELECT * FROM (SELECT a FROM t FOR SHARE) s"])
def test_view_query_refused(q):
    assert operations.check_view_query(q)


def test_shapes():
    assert operations.parse_ops([V, {"op": "drop_view", "name": "v", "reason": "not needed"}])
    for bad in ({"op": "create_view", "name": "v"}, {"op": "create_view", "name": "v", "query": "DELETE FROM t"},
                {"op": "drop_view", "name": "v"}, {"op": "create_view", **{k: V[k] for k in ("name", "query")}, "x": 1}):
        with pytest.raises(operations.OpError):
            operations.parse_ops([bad])
    assert "drop_view" in operations.DESTRUCTIVE_OPS and "create_view" not in operations.DESTRUCTIVE_OPS


def test_create_view_state_sql_and_diff():
    make_table()
    res = gate([V])
    assert res.ok and res.sql == ['CREATE VIEW "master"."v" AS SELECT a FROM t;']
    assert res.diff == ["+ create view v"]
    errs, after = validate(res.operations, introspect.snapshot())
    assert errs == [] and after.views["v"]["tables"] == {"t"}
    assert any("already exists" in e for e in gate([{**V, "name": "t"}]).errors)
    commented = gate([{**V, "comment": "hi"}])
    assert commented.sql[1] == 'COMMENT ON VIEW "master"."v" IS \'hi\';'


def test_naming_rule_applies_to_view_names():
    make_table()
    res = gate([{**V, "name": "BadName"}], skip_rules=False)
    assert any(v["rule"] == "tableNaming" for v in res.violations)


def test_view_applies_reads_and_reverses():
    make_table(rows=[{"a": "x"}, {"a": "y"}])
    res = apply([V])
    assert res.reverse_ops == [{"op": "drop_view", "name": "v", "reason": "revert"}]
    assert rows("SELECT a FROM v ORDER BY a") == [["x"], ["y"]]
    apply(res.reverse_ops)
    assert introspect.snapshot().views == {}


def test_unqualified_names_resolve_in_the_target_schema():
    with rw_connection(autocommit=True) as rw:
        rw.execute("SELECT singularity.create_managed_schema('crm')")
    try:
        make_table(name="t")
        apply([{"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True), col("a")]}], schema="crm")
        apply([{"op": "insert_rows", "table": "t", "rows": [{"t_id": 1, "a": "crm-row"}]}], schema="crm")
        apply([V], schema="crm")
        assert rows("SELECT a FROM crm.v") == [["crm-row"]]
    finally:
        with rw_connection(autocommit=True) as rw:
            rw.execute("DROP SCHEMA IF EXISTS crm CASCADE")


def test_view_cannot_reach_the_app_schema_or_the_filesystem():
    make_table()
    for q in ("SELECT * FROM singularity.accounts_user", "SELECT pg_read_file('/etc/passwd') AS x"):
        try:
            apply([{"op": "create_view", "name": "bad", "query": q}])
        except executor.ApplyError:
            continue                                   # refused at creation: fine
        with pytest.raises(Exception):                 # or unusable afterwards
            rows("SELECT * FROM bad")
        apply([{"op": "drop_view", "name": "bad", "reason": "cleanup"}])


def test_drop_view_needs_reason_and_has_no_reverse():
    make_table()
    apply([V])
    assert any("reason" in e for e in operations_errors({"op": "drop_view", "name": "v"}))
    assert apply([{"op": "drop_view", "name": "v", "reason": "not needed"}]).reverse_ops is None
    assert gate([{"op": "drop_view", "name": "v", "reason": "not needed"}]).errors


def operations_errors(op):
    try:
        operations.parse_ops([op])
    except operations.OpError as e:
        return e.errors
    return []


def test_drop_table_with_dependent_view_fails_cleanly():
    make_table()
    apply([V])
    with pytest.raises(executor.ApplyError, match="depend"):
        apply([{"op": "drop_table", "table": "t", "reason": "test"}])
