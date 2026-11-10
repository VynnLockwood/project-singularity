"""create_enum / add_enum_value / drop_enum and enum column types (Plan B, task 4)."""
import pytest

from gate import operations, sqlgen
from gate.state import validate
from schema import executor, introspect
from schema.util import pg_type_to_allowed

from .ops_helpers import apply, col, gate, make_table, rows

pytestmark = pytest.mark.usefixtures("master")

E_ = {"op": "create_enum", "name": "status", "values": ["new", "done"]}


def errors_of(op):
    try:
        operations.parse_ops([op])
    except operations.OpError as e:
        return e.errors
    return []


def test_shapes():
    assert operations.parse_ops([E_, {"op": "add_enum_value", "name": "status", "value": "late"},
                                 {"op": "drop_enum", "name": "status", "reason": "unused"}])
    for vals in ([], ["a", "a"], [""], [1], ["x" * 64], [str(i) for i in range(101)], "ab"):
        assert errors_of({**E_, "values": vals}), vals
    assert errors_of({**E_, "name": "Bad-Name"}) and errors_of({"op": "add_enum_value", "name": "s", "value": ""})
    assert errors_of({"op": "drop_enum", "name": "status"})
    assert "drop_enum" in operations.DESTRUCTIVE_OPS and "create_enum" not in operations.DESTRUCTIVE_OPS


def test_enum_as_column_type():
    apply([E_])
    apply([{"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True, identity=True), col("s", "status")]}])
    assert introspect.describe_table("t")["columns"][1]["type"].endswith("status")
    apply([{"op": "insert_rows", "table": "t", "rows": [{"s": "new"}]}])
    assert rows("SELECT s::text FROM t") == [["new"]]
    res = gate([{"op": "add_column", "table": "t", "column": col("s2", "nosuchtype")}])
    assert any("not a built-in type or an enum" in e for e in res.errors)
    assert gate([{"op": "create_table", "name": "u", "columns": [col("u_id", "integer"), col("s", "nosuchtype")]}]).errors


def test_enum_created_and_used_in_one_change():
    res = gate([E_, {"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True, identity=True), col("s", "status")]}])
    assert res.ok, res.errors
    assert 'CREATE TYPE "master"."status" AS ENUM (\'new\', \'done\')' in res.sql[0]
    assert '"s" "master"."status"' in res.sql[1]
    apply([E_, {"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True, identity=True), col("s", "status")]}])


def test_add_enum_value_appends_and_rejects_duplicates():
    apply([E_])
    res = apply([{"op": "add_enum_value", "name": "status", "value": "late"}])
    assert res.reverse_ops is None
    assert introspect.snapshot().enums["status"] == ["new", "done", "late"]
    assert gate([{"op": "add_enum_value", "name": "status", "value": "late"}]).errors
    assert gate([{"op": "add_enum_value", "name": "nope", "value": "x"}]).errors
    assert gate([E_]).errors                                     # already exists
    make_table("status_t")
    assert gate([{**E_, "name": "status_t"}]).errors              # a table already has that name


def test_create_enum_reverse_is_drop_enum_while_unused():
    res = apply([E_])
    assert res.reverse_ops == [{"op": "drop_enum", "name": "status", "reason": "revert"}]
    apply(res.reverse_ops)
    assert introspect.snapshot().enums == {}
    apply([E_])
    apply([{"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True, identity=True), col("s", "status")]}])
    blocked = gate([{"op": "drop_enum", "name": "status", "reason": "revert"}])
    assert any("t.s" in e for e in blocked.errors)
    assert introspect.snapshot().enums == {"status": ["new", "done"]}


def test_new_value_used_in_the_same_change_is_refused_by_postgres():
    apply([E_])
    apply([{"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True, identity=True), col("s", "status")]}])
    with pytest.raises(executor.ApplyError):
        apply([{"op": "add_enum_value", "name": "status", "value": "late"},
               {"op": "insert_rows", "table": "t", "rows": [{"s": "late"}]}])
    assert introspect.snapshot().enums["status"] == ["new", "done"]


def test_alter_column_type_to_enum_reverses_to_the_old_type():
    apply([E_])
    make_table("t", extra=[col("s")])
    res = apply([{"op": "alter_column_type", "table": "t", "column": "s", "type": "status"}])
    assert res.reverse_ops == [{"op": "alter_column_type", "table": "t", "column": "s", "type": "text"}]
    back = apply([{"op": "alter_column_type", "table": "t", "column": "s", "type": "text"}])
    assert back.reverse_ops == [{"op": "alter_column_type", "table": "t", "column": "s", "type": "status"}]


def test_enum_names_follow_the_naming_rule():
    assert gate([E_], skip_rules=False).ok
    res = gate([{"op": "create_enum", "name": "order", "values": ["a"]}], skip_rules=False)
    assert any(v["rule"] == "tableNaming" for v in res.violations)         # reserved word


def test_drop_enum_needs_reason():
    assert any("reason" in e for e in errors_of({"op": "drop_enum", "name": "status"}))


def test_diff_lines():
    assert sqlgen.describe(E_) == "+ create enum status (new, done)"
    assert sqlgen.describe({"op": "add_enum_value", "name": "status", "value": "late"}) == "+ add value 'late' to enum status"
    assert sqlgen.describe({"op": "drop_enum", "name": "status", "reason": "unused"}) == "- drop enum status  (reason: unused)"


def test_pg_type_to_allowed_knows_enums():
    assert pg_type_to_allowed("master.status", enums={"status"}, schema="master") == "status"
    assert pg_type_to_allowed("status", enums={"status"}) == "status"
    assert pg_type_to_allowed("money") is None
