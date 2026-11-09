import pytest

from gate import check, sqlgen
from gate.defaults import DEFAULT_RULES

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
LONG = "a" + "b" * 62  # 63 characters


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def st(*tables):
    return {t: {"columns": {"id": {"type": "integer", "nullable": False}, "name": {"type": "text", "nullable": True},
                            "updated_at": {}, "created_by": {}, "is_active": {}},
                "indexes": {"ix_t"}, "constraints": {"c_t"}, "pk": "id",
                "roles": {"updated_at": "updated_at", "created_by": "created_by", "is_active": "is_active"}} for t in tables}


def all_ops():
    return [
        {"op": "add_column", "table": "t", "column": col("x", comment="hi")},
        {"op": "set_column_comment", "table": "t", "column": "name", "comment": "n"},
        {"op": "rename_table", "table": "t", "new_name": "t2"},
        {"op": "set_table_comment", "table": "t2", "comment": "c"},
        {"op": "add_foreign_key", "table": "t2", "column": "id", "ref_table": "u", "ref_column": "id"},
        {"op": "add_check", "table": "u", "name": "chk", "expression": "length(name) > 1"},
        {"op": "drop_index", "name": "ix_t", "reason": "r"},
        {"op": "insert_rows", "table": "u", "rows": [{"name": "a"}]},
        {"op": "update_rows", "table": "u", "where": {"id": 1}, "set": {"name": "b"}},
        {"op": "deactivate_rows", "table": "u", "where": {"id": 1}},
        {"op": "drop_constraint", "table": "u", "name": "c_t", "reason": "r"},
        {"op": "drop_column", "table": "u", "column": "name", "reason": "r"},
        {"op": "drop_table", "table": "u", "reason": "r"},
    ]


def create_ref():
    return {"op": "create_table", "name": "child", "comment": "tbl",
            "columns": [col("child_code", unique=True, comment="cc"),
                        col("parent_id", "integer", references={"table": "parent", "column": "id"})]}


@pytest.mark.parametrize("schema", ["crm", LONG])
def test_every_statement_is_qualified_with_the_schema(schema):
    ops = all_ops() + [create_ref()]
    out = []
    for op in ops:
        state = st("t", "u", "parent", "t2")
        out += [s.sql.as_string(None) for s in sqlgen.statements_for(op, state, "me", None, schema)]
    assert out
    text = "\n".join(out)
    assert "master" not in text
    assert f'"{schema}".' in text
    for s in out:
        assert f'"{schema}".' in s, s
    assert f'REFERENCES "{schema}"."parent"' in text
    assert f'DROP INDEX "{schema}"."ix_t"' in text


def test_default_schema_is_master_golden():
    st_ = {"customer": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    op = {"op": "add_foreign_key", "table": "customer", "column": "id", "ref_table": "customer"}
    res = check.run([op], st_, RULES)
    assert 'ADD CONSTRAINT "fk_customer_id" FOREIGN KEY ("id") REFERENCES "master"."customer" ("id")' in res.sql[0]
    assert res.sql == check.run([op], st_, RULES, schema="master").sql
    assert res.sql[0] == (
        'ALTER TABLE "master"."customer" ADD CONSTRAINT "fk_customer_id" FOREIGN KEY ("id") '
        'REFERENCES "master"."customer" ("id");')
    assert sqlgen.tbl("t").as_string(None) == '"master"."t"'


def test_check_run_forwards_schema():
    st_ = {"customer": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    op = {"op": "add_foreign_key", "table": "customer", "column": "id", "ref_table": "customer"}
    res = check.run([op], st_, RULES, schema="crm")
    assert res.ok and "master" not in res.sql[0] and '"crm"."customer"' in res.sql[0]


def test_schema_name_is_escaped_as_identifier():
    assert sqlgen.tbl("t", 'we"ird').as_string(None) == '"we""ird"."t"'


def test_database_ops_render_select_statements():
    ms = {"master": 0, "crm": 0}
    r = check.run([{"op": "create_schema", "name": "hr"}], {}, RULES, managed_schemas=ms)
    assert r.ok and r.sql == ["SELECT singularity.create_managed_schema('hr');"]
    assert r.diff == ["+ create schema hr"]
    r = check.run([{"op": "rename_schema", "schema": "crm", "new_name": "sales"}], {}, RULES, managed_schemas=ms)
    assert r.sql == ["SELECT singularity.rename_managed_schema('crm', 'sales');"]
    r = check.run([{"op": "drop_schema", "schema": "crm", "reason": "unused"}], {}, RULES, managed_schemas=ms)
    assert r.sql == ["SELECT singularity.drop_managed_schema('crm');"]
    assert r.diff == ["- drop schema crm  (reason: unused)"]
    assert sqlgen.describe({"op": "rename_schema", "schema": "a", "new_name": "b"}) == "~ rename schema a -> b"


def test_cross_schema_foreign_keys_are_refused():
    crm = st("customer")
    # a schema-qualified reference is not a valid identifier at all
    for ref in ("master.district", "other.customer"):
        res = check.run([{"op": "create_table", "name": "t",
                          "columns": [col("t_code", unique=True),
                                      col("d", "integer", references={"table": ref, "column": "id"})]}],
                        crm, RULES, schema="crm")
        assert not res.ok and not res.sql
        assert any("references.table must be an identifier" in e for e in res.errors), res.errors
    res = check.run([{"op": "add_foreign_key", "table": "customer", "column": "id", "ref_table": "master.district",
                      "ref_column": "id"}], crm, RULES, schema="crm")
    assert not res.ok and any("ref_table must be an identifier" in e for e in res.errors), res.errors
    # an unqualified table that only exists in another schema is unknown in this one
    res = check.run([{"op": "create_table", "name": "t",
                      "columns": [col("t_code", unique=True),
                                  col("d", "integer", references={"table": "district", "column": "id"})]}],
                    crm, RULES, schema="crm")
    assert not res.ok and any("references missing table 'district'" in e for e in res.errors), res.errors
    res = check.run([{"op": "add_foreign_key", "table": "customer", "column": "id", "ref_table": "district",
                      "ref_column": "id"}], crm, RULES, schema="crm")
    assert not res.ok and any("referenced table 'district' does not exist" in e for e in res.errors), res.errors

