"""set/drop column default and not-null (Plan B, task 2)."""
import pytest

from gate import operations, sqlgen
from gate.state import validate
from schema import executor, introspect
from schema.util import pg_default_to_allowed

from .ops_helpers import RULES, apply, col, gate, make_table, rows

pytestmark = pytest.mark.usefixtures("master")


def D(table="t", column="a"):
    return {"table": table, "column": column}


def test_shapes():
    ok = [{"op": "set_column_default", **D(), "default": "x"}, {"op": "set_column_default", **D(), "default": "now()"},
          {"op": "set_column_default", **D(), "default": 5}, {"op": "drop_column_default", **D()},
          {"op": "set_not_null", **D()}, {"op": "drop_not_null", **D()}]
    assert operations.parse_ops(ok) == ok
    for bad in ({"op": "set_column_default", **D(), "default": {"a": 1}}, {"op": "set_column_default", **D()},
                {"op": "set_not_null", **D(), "extra": 1}, {"op": "drop_column_default", "table": "t"}):
        with pytest.raises(operations.OpError):
            operations.parse_ops([bad])


def test_set_default_state_and_sql():
    make_table()
    res = gate([{"op": "set_column_default", **D(), "default": "x"}])
    assert res.ok and res.sql == ['ALTER TABLE "master"."t" ALTER COLUMN "a" SET DEFAULT \'x\';']
    errs, after = validate(res.operations, introspect.snapshot())
    assert errs == [] and after["t"]["columns"]["a"]["default"] == "x"
    assert gate([{"op": "set_column_default", "table": "t", "column": "nope", "default": 1}]).errors


def test_default_functions_render_raw():
    make_table(extra=[col("at", "timestamptz")])
    res = gate([{"op": "set_column_default", "table": "t", "column": "at", "default": "now()"}])
    assert res.sql == ['ALTER TABLE "master"."t" ALTER COLUMN "at" SET DEFAULT now();']


def test_set_and_drop_default_apply_and_reverse():
    make_table()
    r1 = apply([{"op": "set_column_default", **D(), "default": "x"}])
    assert r1.reverse_ops == [{"op": "drop_column_default", **D()}]
    assert introspect.describe_table("t")["columns"][1]["default"].startswith("'x'")
    r2 = apply([{"op": "set_column_default", **D(), "default": "y"}])
    assert r2.reverse_ops == [{"op": "set_column_default", **D(), "default": "x"}]
    apply(r2.reverse_ops)
    assert introspect.describe_table("t")["columns"][1]["default"].startswith("'x'")
    r3 = apply([{"op": "drop_column_default", **D()}])
    assert r3.reverse_ops == [{"op": "set_column_default", **D(), "default": "x"}]
    apply(r1.reverse_ops)
    assert introspect.describe_table("t")["columns"][1]["default"] is None


def test_default_with_unrepresentable_previous_value_is_irreversible(master):
    make_table()
    from schema.conn import rw_connection
    with rw_connection(autocommit=True) as c:
        c.execute("CREATE SEQUENCE master.s")
        c.execute("ALTER TABLE master.t ALTER COLUMN a SET DEFAULT nextval('master.s')::text")
    assert apply([{"op": "drop_column_default", **D()}]).reverse_ops is None


def test_set_not_null_fails_with_null_count():
    make_table(rows=[{"a": None}, {"a": None}, {"a": None}])
    with pytest.raises(executor.ApplyError, match=r"3 row\(s\)"):
        apply([{"op": "set_not_null", **D()}])


def test_set_not_null_ok_and_reverse_is_drop_not_null():
    make_table(rows=[{"a": "x"}])
    res = apply([{"op": "set_not_null", **D()}])
    assert res.reverse_ops == [{"op": "drop_not_null", **D()}]
    assert introspect.describe_table("t")["columns"][1]["nullable"] is False
    apply(res.reverse_ops)
    assert introspect.describe_table("t")["columns"][1]["nullable"] is True


def test_drop_not_null_refused_on_primary_key():
    make_table()
    res = gate([{"op": "drop_not_null", "table": "t", "column": "t_id"}])
    assert any("primary key" in e for e in res.errors)


def test_drop_not_null_reverse_is_set_not_null():
    make_table()
    apply([{"op": "set_not_null", **D()}])
    assert apply([{"op": "drop_not_null", **D()}]).reverse_ops == [{"op": "set_not_null", **D()}]


def test_diff_lines():
    assert sqlgen.describe({"op": "set_column_default", **D(), "default": 1}) == "~ set default of t.a"
    assert sqlgen.describe({"op": "drop_column_default", **D()}) == "~ drop default of t.a"
    assert sqlgen.describe({"op": "set_not_null", **D()}) == "~ set t.a not null"
    assert sqlgen.describe({"op": "drop_not_null", **D()}) == "~ allow nulls in t.a"


@pytest.mark.parametrize("expr,typ,want", [
    ("'x'::text", "text", (True, "x")), ("5", "integer", (True, 5)), ("true", "boolean", (True, True)),
    ("now()", "timestamp with time zone", (True, "now()")), ("CURRENT_DATE", "date", (True, "current_date")),
    ("gen_random_uuid()", "uuid", (True, "gen_random_uuid()")), (None, "text", (True, None)),
    ("nextval('s'::regclass)", "integer", (False, None)), ("'it''s'::text", "text", (True, "it's")),
    ("1.5", "numeric", (True, 1.5)),
])
def test_pg_default_to_allowed(expr, typ, want):
    assert pg_default_to_allowed(expr, typ) == want


def test_enum_cast_defaults_map_back_to_a_plain_value():
    from schema.util import pg_default_to_allowed
    assert pg_default_to_allowed("'new'::master.status", "status") == (True, "new")
    assert pg_default_to_allowed("'new'::status", "status") == (True, "new")
