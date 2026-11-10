"""Final-review fixes for plan B (each test was watched failing first)."""
import pytest

from accounts.models import Role, User
from approvals import service
from gate import operations
from schema import introspect
from scripts.sqlparse import parse_script

from .ops_helpers import apply, col, gate, gate_db, make_table, rows
from .test_ops_roundtrip import facts

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def run(approver, ops):
    return service.approve(approver, service.propose(approver, ops, source="web").pk)


def test_view_and_enum_ops_keep_the_affected_tables():
    got = operations.affected_tables([
        {"op": "create_view", "name": "v", "query": "SELECT a FROM t"},
        {"op": "add_enum_value", "name": "s", "value": "x"},
        {"op": "drop_view", "name": "w", "reason": "r"},
        {"op": "create_enum", "name": "e", "values": ["a"]},
        {"op": "drop_enum", "name": "e", "reason": "r"},
        {"op": "add_column", "table": "u", "column": col("q")}])
    assert got == ["t", "u"]


def test_reverting_a_no_op_not_null_change_restores_the_original_state(approver):
    make_table(extra=[col("a"), col("b", nullable=False)])
    before = facts()
    ch = run(approver, [{"op": "set_not_null", "table": "t", "column": "b"},
                        {"op": "drop_not_null", "table": "t", "column": "a"},
                        {"op": "set_column_default", "table": "t", "column": "a", "default": "x"}])
    assert ch.status == "applied", ch.error
    rev = service.revert(approver, ch.pk)
    assert rev.status == "applied", rev.error
    assert facts() == before


def test_delete_that_cascades_is_not_offered_as_reversible(approver):
    make_table("p", extra=[col("code", unique=True)], rows=[{"code": "A"}])
    make_table("c", extra=[col("p_id", "integer")])
    apply([{"op": "add_foreign_key", "table": "c", "column": "p_id", "ref_table": "p", "ref_column": "p_id", "on_delete": "cascade"}])
    ch = run(approver, [{"op": "delete_rows", "table": "p", "where": {"code": ["A"]}, "reason": "test"}])
    assert ch.status == "applied", ch.error
    with pytest.raises(service.ChangeError):
        service.revert(approver, ch.pk)


def test_upsert_with_a_null_key_is_refused_by_the_shape_check():
    make_table(extra=[col("code", unique=True), col("name")])
    res = gate([{"op": "upsert_rows", "table": "t", "key": ["code"], "columns": ["name"],
                 "rows": [{"code": None, "name": "x"}]}])
    assert not res.ok and any("key" in e and "null" in e.lower() for e in res.errors)


def test_move_table_with_an_enum_column_is_refused():
    from schema.conn import rw_connection
    with rw_connection(autocommit=True) as rw:
        rw.execute("SELECT singularity.create_managed_schema('crm')")
    try:
        apply([{"op": "create_enum", "name": "status", "values": ["a", "b"]}])
        make_table(extra=[col("s", "status")])
        res = gate_db([{"op": "move_table", "table": "t", "from_schema": "master", "to_schema": "crm"}])
        assert not res.ok and any("enum" in e and "status" in e for e in res.errors)
    finally:
        with rw_connection(autocommit=True) as rw:
            rw.execute("DROP SCHEMA IF EXISTS crm CASCADE")


def test_view_with_a_qualified_enum_cast_has_the_schema_stripped():
    p = parse_script("CREATE TYPE public.status AS ENUM ('new');\nCREATE TABLE public.i (s public.status);\n"
                     "CREATE VIEW public.v AS SELECT i.s FROM public.i WHERE i.s = 'new'::public.status;")
    assert p.errors == []
    v = next(o for o in p.ops if o["op"] == "create_view")
    assert "public" not in v["query"]


def test_string_default_regex_is_a_clean_raw_string():
    import warnings
    import importlib
    import schema.util as u
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        src = open(u.__file__).read()
        compile(src, u.__file__, "exec")
