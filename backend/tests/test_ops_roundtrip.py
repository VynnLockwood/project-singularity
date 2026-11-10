"""Plan B wrap-up: every new op is described, visible to the AI, and reversible where it should be (task 8)."""
import pytest

from accounts.models import Role, User
from approvals import service
from gate import sqlgen
from mcp_server import tools
from chat import toolspecs
from schema import introspect
from schema.conn import rw_connection

from .ops_helpers import apply, col, make_table, rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

NEW_OPS = ["set_column_default", "drop_column_default", "set_not_null", "drop_not_null", "create_view", "drop_view",
           "create_enum", "add_enum_value", "drop_enum", "delete_rows", "upsert_rows", "move_table"]


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


@pytest.fixture
def viewer():
    return User.objects.create(username="v@x.com", email="v@x.com", role=Role.VIEWER)


def test_every_new_op_is_listed_in_the_descriptions_and_has_an_example():
    chat_text = toolspecs.SPECS["propose_change"][0]
    for op in NEW_OPS:
        assert op in tools.OPERATIONS_HELP and op in chat_text, op
    examples = tools.get_rules(None)["operations"]
    assert set(examples) == set(NEW_OPS)
    for op, ex in examples.items():
        assert ex["op"] == op


def test_get_rules_examples_pass_the_shape_check():
    from gate import operations
    for ex in tools.get_rules(None)["operations"].values():
        operations.parse_ops([ex])


def test_list_tables_reports_views_and_enums(viewer):
    make_table()
    apply([{"op": "create_enum", "name": "status", "values": ["new", "done"]},
           {"op": "create_view", "name": "v", "query": "SELECT a FROM t", "comment": "all a"}])
    got = tools.list_tables(viewer)
    assert [t["name"] for t in got["tables"]] == ["t"]
    assert got["views"] == [{"name": "v", "comment": "all a"}]
    assert got["enums"] == [{"name": "status", "values": ["new", "done"]}]


def facts(schema="master"):
    snap = introspect.snapshot(schema)
    tables = {t: {"columns": {c: (e["type"], e["nullable"], e["default"]) for c, e in v["columns"].items()},
                  "keys": sorted(sorted(k) for k in v["keys"])} for t, v in snap.items()}
    data = {t: rows(f"SELECT * FROM {schema}.{t} ORDER BY 1") for t in snap}
    return tables, sorted(snap.views), dict(snap.enums), data


def _setup_t(default=False, not_null=False):
    make_table(extra=[col("code", unique=True), col("name")], rows=[
        {"code": "A", "name": "old"}, {"code": "B", "name": "keep"}, {"code": "C", "name": "gone"}])
    if default:
        apply([{"op": "set_column_default", "table": "t", "column": "name", "default": "dflt"}])
    if not_null:
        apply([{"op": "set_not_null", "table": "t", "column": "name"}])


CASES = {
    "set_column_default": (lambda: _setup_t(), [{"op": "set_column_default", "table": "t", "column": "name", "default": "x"}]),
    "drop_column_default": (lambda: _setup_t(default=True), [{"op": "drop_column_default", "table": "t", "column": "name"}]),
    "set_not_null": (lambda: _setup_t(), [{"op": "set_not_null", "table": "t", "column": "name"}]),
    "drop_not_null": (lambda: _setup_t(not_null=True), [{"op": "drop_not_null", "table": "t", "column": "name"}]),
    "create_view": (lambda: _setup_t(), [{"op": "create_view", "name": "v", "query": "SELECT name FROM t"}]),
    "create_enum": (lambda: _setup_t(), [{"op": "create_enum", "name": "status", "values": ["a", "b"]}]),
    "delete_rows": (lambda: _setup_t(), [{"op": "delete_rows", "table": "t", "where": {"code": ["A", "C"]}, "reason": "roundtrip test"}]),
    "upsert_rows": (lambda: _setup_t(), [{"op": "upsert_rows", "table": "t", "key": ["code"], "columns": ["name"],
                                          "rows": [{"code": "A", "name": "new"}, {"code": "Z", "name": "fresh"}]}]),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_apply_then_revert_leaves_the_database_unchanged(name, approver):
    setup, ops = CASES[name]
    setup()
    before = facts()
    ch = service.approve(approver, service.propose(approver, ops, source="web").pk)
    assert ch.status == "applied", ch.error
    assert facts() != before
    rev = service.revert(approver, ch.pk)
    assert rev.status == "applied", rev.error
    assert facts() == before


def test_move_table_round_trip(approver):
    with rw_connection(autocommit=True) as rw:
        rw.execute("SELECT singularity.create_managed_schema('crm')")
    try:
        _setup_t()
        before = facts()
        ch = service.approve(approver, service.propose(approver, [
            {"op": "move_table", "table": "t", "from_schema": "master", "to_schema": "crm"}], source="web").pk)
        assert ch.status == "applied", ch.error
        rev = service.revert(approver, ch.pk)
        assert rev.status == "applied", rev.error
        assert facts() == before
    finally:
        with rw_connection(autocommit=True) as rw:
            rw.execute("DROP SCHEMA IF EXISTS crm CASCADE")


@pytest.mark.parametrize("op", [
    {"op": "drop_view", "name": "v", "reason": "round trip"},
    {"op": "add_enum_value", "name": "status", "value": "c"},
    {"op": "drop_enum", "name": "status", "reason": "round trip"},
])
def test_irreversible_ops_say_so(op, approver):
    _setup_t()
    apply([{"op": "create_view", "name": "v", "query": "SELECT name FROM t"}, {"op": "create_enum", "name": "status", "values": ["a", "b"]}])
    ch = service.approve(approver, service.propose(approver, [op], source="web").pk)
    assert ch.status == "applied", ch.error
    with pytest.raises(service.ChangeError):
        service.revert(approver, ch.pk)


EXAMPLES = {
    "set_column_default": {"table": "customer", "column": "status", "default": "new"},
}


def test_every_op_has_a_diff_line_and_sql():
    ex = tools.get_rules(None)["operations"]
    for op in NEW_OPS:
        line = sqlgen.describe(ex[op])
        assert line != op and line[0] in "+-~", op
        if op != "move_table":
            stmts = sqlgen.statements_for(ex[op], {}, "me@x.com", None, "master")
            assert stmts, op
