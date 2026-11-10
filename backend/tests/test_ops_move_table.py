"""move_table (Plan B, task 7)."""
import pytest

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from gate import operations, sqlgen
from schema import introspect
from schema.conn import rw_connection

from .ops_helpers import ME, apply, col, gate_db, make_table, rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

MOVE = {"op": "move_table", "table": "t", "from_schema": "master", "to_schema": "crm"}


@pytest.fixture
def crm(master):
    with rw_connection(autocommit=True) as rw:
        rw.execute("SELECT singularity.create_managed_schema('crm')")
    yield "crm"
    with rw_connection(autocommit=True) as rw:
        rw.execute("DROP SCHEMA IF EXISTS crm CASCADE")


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


@pytest.fixture
def editor():
    return User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)


def errors_of(op):
    try:
        operations.parse_ops([op])
    except operations.OpError as e:
        return e.errors
    return []


def test_shape():
    assert operations.parse_ops([MOVE]) and "move_table" in operations.DATABASE_OPS
    assert errors_of({**MOVE, "to_schema": "Bad Name"}) and errors_of({**MOVE, "x": 1})
    assert errors_of({k: v for k, v in MOVE.items() if k != "to_schema"})
    assert any("alone" in e for e in errors_of_many([MOVE, {"op": "add_column", "table": "t", "column": col("c")}]))


def errors_of_many(ops):
    try:
        operations.parse_ops(ops)
    except operations.OpError as e:
        return e.errors
    return []


def test_valid_move_passes_the_gate(crm):
    make_table()
    res = gate_db([MOVE])
    assert res.ok, res.errors
    assert res.diff == ["~ move table t from master to crm"]
    assert res.sql == ['ALTER TABLE "master"."t" SET SCHEMA "crm";']


def test_refusals_name_the_cause(crm):
    make_table()
    def msgs(op):
        return " | ".join(gate_db([op]).errors)
    assert "does not exist" in msgs({**MOVE, "table": "nope"})
    assert "not managed" in msgs({**MOVE, "to_schema": "nowhere"})
    assert "same" in msgs({**MOVE, "to_schema": "master"})
    # same-name table, view and enum in the target
    apply([{"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True)]}], schema="crm")
    assert "already has" in msgs(MOVE)
    # dependent view
    make_table("u")
    apply([{"op": "create_view", "name": "uv", "query": "SELECT a FROM u"}])
    assert "uv" in msgs({**MOVE, "table": "u"}) and "depend" in msgs({**MOVE, "table": "u"})
    # foreign keys
    make_table("p")
    apply([{"op": "create_table", "name": "c", "columns": [col("c_id", "integer", primary_key=True),
                                                            col("p_id", "integer", references={"table": "p"})]}])
    assert "foreign key" in msgs({**MOVE, "table": "c"}) and "foreign key" in msgs({**MOVE, "table": "p"})


def test_view_in_target_blocks_the_name(crm):
    make_table()
    apply([{"op": "create_table", "name": "base", "columns": [col("base_id", "integer", primary_key=True)]}], schema="crm")
    apply([{"op": "create_view", "name": "t", "query": "SELECT base_id FROM base"}], schema="crm")
    assert "already has" in " ".join(gate_db([MOVE]).errors)


def test_move_table_moves_rows_and_reverse_moves_back(crm, approver):
    make_table(rows=[{"a": "x"}, {"a": "y"}])
    ch = service.propose(approver, [MOVE], source="web")
    assert ch.status == "pending" and ch.kind == "schema", ch.check_result
    ch = service.approve(approver, ch.pk)
    assert ch.status == "applied", ch.error
    assert rows("SELECT a FROM crm.t ORDER BY a") == [["x"], ["y"]]
    assert "t" not in introspect.snapshot("master") and "t" in introspect.snapshot("crm")
    apply([{"op": "insert_rows", "table": "t", "rows": [{"a": "z"}]}], schema="crm")      # identity sequence moved too
    assert rows("SELECT t_id FROM crm.t ORDER BY 1") == [[1], [2], [3]]
    rev = service.revert(approver, ch.pk)
    assert rev.status == "applied", rev.error
    assert rows("SELECT count(*) FROM t") == [[3]]


def test_move_table_service_flow_backs_up_and_goes_stale(crm, editor, approver):
    make_table(rows=[{"a": "x"}])
    ch = service.propose(editor, [MOVE], source="web")
    assert ch.status == "pending"
    apply([{"op": "add_column", "table": "t", "column": col("extra")}])            # the table changed since the check
    stale = service.approve(approver, ch.pk)
    assert stale.status == Change.Status.STALE
    fresh = service.propose(editor, [MOVE], source="web")
    done = service.approve(approver, fresh.pk)
    assert done.status == "applied"
    from audit.models import AuditLog
    assert AuditLog.objects.filter(action="backup.created").count() >= 2             # both schemas
