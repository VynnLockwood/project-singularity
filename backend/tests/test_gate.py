import pytest

from gate import check, operations, rules, sqlgen, state
from gate.defaults import DEFAULT_RULES

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def create(name="customer", cols=None):
    return {"op": "create_table", "name": name, "columns": cols or [col("customer_code", unique=True), col("name")]}


# ---- operations.parse_ops --------------------------------------------------

def test_valid_create_table_parses():
    assert operations.parse_ops([create()])


@pytest.mark.parametrize(
    "bad",
    [
        [],
        "x",
        [{"op": "nope"}],
        [{"op": "create_table", "name": "a b", "columns": [col("x")]}],        # bad identifier
        [{"op": "create_table", "name": 'a"; drop', "columns": [col("x")]}],   # injection attempt
        [{"op": "create_table", "name": "t", "columns": [col("x", "text; drop table y")]}],
        [{"op": "create_table", "name": "t", "columns": [col("x"), col("x")]}],  # duplicate column
        [{"op": "create_table", "name": "t", "columns": [col("x", default={"a": 1})]}],     # default must be a scalar
        [{"op": "update_rows", "table": "t", "where": {}, "set": {"a": 1}}],     # no filter
        [{"op": "update_rows", "table": "t", "where": {"a": None}, "set": {"a": 1}}],
        [{"op": "drop_table", "table": "t"}],                                     # no reason
        [{"op": "drop_column", "table": "t", "column": "c", "reason": " "}],
        [{"op": "insert_rows", "table": "t", "rows": []}],
        [{"op": "add_column", "table": "t", "column": col("x"), "surprise": 1}],  # unknown key
    ],
)
def test_malformed_ops_rejected(bad):
    with pytest.raises(operations.OpError):
        operations.parse_ops(bad)


def test_unknown_type_is_refused_by_the_gate():
    """A lowercase word may be an enum name, so the shape check lets it through; the state check refuses it."""
    res = check.run([{"op": "create_table", "name": "t", "columns": [col("x", "money")]}], {}, RULES, skip_rules=True)
    assert any("unsupported type 'money'" in e for e in res.errors)


def test_op_limit():
    with pytest.raises(operations.OpError):
        operations.parse_ops([create()] * 3, max_ops=2)


def test_all_errors_reported_at_once():
    with pytest.raises(operations.OpError) as e:
        operations.parse_ops([{"op": "drop_table", "table": "t"}, {"op": "bogus"}])
    assert len(e.value.errors) == 2


def test_kind_and_affected_tables():
    assert operations.kind_of([{"op": "insert_rows", "table": "t", "rows": [{"a": 1}]}]) == "data"
    assert operations.kind_of([create()]) == "schema"
    ops = [
        create("order", [col("customer_id", "integer", references={"table": "customer"})]),
        {"op": "rename_table", "table": "a", "new_name": "b"},
    ]
    assert operations.affected_tables(ops) == ["a", "b", "customer", "order"]


# ---- naming helpers --------------------------------------------------------

@pytest.mark.parametrize(
    "name,style,expected",
    [
        ("supplier_contact", "camelCase", "supplierContact"),
        ("SupplierContact", "camelCase", "supplierContact"),
        ("supplierContact", "snake_case", "supplier_contact"),
        ("supplier_contact", "PascalCase", "SupplierContact"),
        ("taxID", "snake_case", "tax_id"),
        ("customerId", "snake_case", "customer_id"),
    ],
)
def test_to_style(name, style, expected):
    assert rules.to_style(name, style) == expected


def test_matches_style():
    assert rules.matches_style("supplierContact", "camelCase")
    assert not rules.matches_style("Supplier", "camelCase")
    assert not rules.matches_style("supplier_contact", "camelCase")
    assert rules.matches_style("supplier_contact", "snake_case")


# ---- rules -----------------------------------------------------------------

def run(ops, st=None, rl=RULES, **kw):
    return check.run(ops, st or {}, rl, **kw)


def names(res, sev=None):
    return {v["rule"] for v in res.violations if sev is None or v["severity"] == sev}


def test_audit_columns_are_added_automatically():
    res = run([create()])
    cols = [c["name"] for c in res.operations[0]["columns"]]
    assert cols[:5] == ["customer_id", "created_at", "updated_at", "created_by", "is_active"]
    assert "customer.customer_id" in res.auto_added
    assert res.ok


def test_original_ops_not_mutated():
    ops = [create()]
    run(ops)
    assert [c["name"] for c in ops[0]["columns"]] == ["customer_code", "name"]


def test_table_naming_blocks_with_suggestion():
    res = run([create("supplierContact")])
    v = next(v for v in res.violations if v["rule"] == "tableNaming")
    assert v["severity"] == "block" and v["suggestion"] == "supplier_contact"
    assert not res.ok


def test_column_naming_blocks():
    res = run([create(cols=[col("customer_code", unique=True), col("phoneNumber")])])
    assert "columnNaming" in names(res, "block") and not res.ok


def test_reserved_word_blocked():
    res = run([create("order")])
    assert "tableNaming" in names(res, "block")


def test_money_must_be_numeric_18_2():
    bad = run([create(cols=[col("customer_code", unique=True), col("unit_price", "real")])])
    assert {"moneyIsNumeric", "noFloat"} <= names(bad, "block")
    ok = run([create(cols=[col("customer_code", unique=True), col("unit_price", "numeric(18,2)")])])
    assert ok.ok


def test_timestamp_columns_need_timezone():
    res = run([create(cols=[col("customer_code", unique=True), col("shipped_at", "timestamp")])])
    assert "timestampsHaveTimezone" in names(res, "block")


def test_wrong_type_for_required_column_blocks():
    res = run([create(cols=[col("customer_id", "text"), col("customer_code", unique=True)])])
    assert "auditColumns" in names(res, "block")


def test_required_columns_without_autoadd_is_violation():
    rl = [{**r, "enabled": True} for r in DEFAULT_RULES]
    for r in rl:
        if r["name"] == "auditColumns":
            r["params"] = {**r["params"], "autoAdd": False}
    res = run([create()], rl=rl)
    assert "auditColumns" in names(res, "block") and not res.auto_added


def test_code_column_free_text_warns_but_unique_key_does_not():
    res = run([create(cols=[col("customer_code", unique=True), col("country_code")])])
    msgs = [v["message"] for v in res.violations if v["rule"] == "codesNeedLookup"]
    assert len(msgs) == 1 and "country_code" in msgs[0]
    assert res.ok  # warnings never block


def test_fk_naming_warns_with_suggestion():
    st = {"customer": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    op = create("invoice", [col("invoiceCode", unique=True), col("buyer", "integer", references={"table": "customer"})])
    res = run([op], st)
    v = next(v for v in res.violations if v["rule"] == "foreignKeyNaming")
    assert v["suggestion"] == "customer_id"


def test_business_key_warning():
    res = run([create(cols=[col("name")])])
    assert "needsBusinessKey" in names(res, "warn")


def test_drops_warn():
    st = {"customer": {"columns": {"id": {"type": "integer", "nullable": False}, "x": {"type": "text", "nullable": True}},
                       "indexes": set(), "constraints": set()}}
    res = run([{"op": "drop_column", "table": "customer", "column": "x", "reason": "unused"}], st)
    assert "careWithDrops" in names(res, "warn") and res.ok


def test_disabled_rule_is_ignored():
    rl = [{**r, "enabled": r["name"] != "tableNaming"} for r in DEFAULT_RULES]
    assert "tableNaming" not in names(run([create("Bad_Name")], rl=rl))


def test_snake_case_project_config():
    rl = [{**r, "enabled": True, "params": {**r["params"], **({"style": "snake_case"} if r["type"] in ("naming", "fk_naming") else {})}}
          for r in DEFAULT_RULES]
    res = run([create("supplier_contact", [col("supplier_code", unique=True)])], rl=rl)
    # snake_case names pass naming; required camelCase audit columns are still auto-added
    assert "tableNaming" not in names(res) and "columnNaming" not in names(res)
    assert "tableNaming" in names(run([create("supplierContact")], rl=rl), "block")


@pytest.mark.parametrize(
    "rtype,params,ok",
    [
        ("naming", {"target": "table", "style": "camelCase"}, True),
        ("naming", {"target": "view", "style": "camelCase"}, False),
        ("naming", {"target": "table", "style": "kebab"}, False),
        ("type_map", {"columnPattern": "(", "allowedTypes": ["text"]}, False),
        ("type_map", {"columnPattern": "x"}, False),
        ("forbidden", {"ops": []}, False),
        ("required_columns", {"columns": [col("id", "money")]}, False),
        ("business_key", {}, True),
        ("nope", {}, False),
    ],
)
def test_rule_definition_validation(rtype, params, ok):
    assert (rules.validate_rule_definition(rtype, params, "warn") == []) is ok


def test_rule_severity_validated():
    assert rules.validate_rule_definition("business_key", {}, "fatal")


# ---- state / semantic validation ------------------------------------------

def test_semantic_errors():
    errs, _ = state.validate([{"op": "add_column", "table": "ghost", "column": col("x")}], {})
    assert errs and "ghost" in errs[0]


def test_later_ops_can_use_earlier_ones():
    ops = [
        create("customer", [col("id", "integer")]),
        {"op": "add_column", "table": "customer", "column": col("email")},
        {"op": "rename_column", "table": "customer", "column": "email", "new_name": "mail"},
        {"op": "add_index", "table": "customer", "columns": ["mail"]},
    ]
    errs, after = state.validate(ops, {})
    assert errs == [] and "mail" in after["customer"]["columns"]


def test_duplicate_table_and_missing_fk_target():
    st = {"customer": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    errs, _ = state.validate([create("customer", [col("id", "integer")])], st)
    assert any("already exists" in e for e in errs)
    errs, _ = state.validate([create("t", [col("x", "integer", references={"table": "nope"})])], {})
    assert any("missing table" in e for e in errs)


def test_deactivate_requires_is_active():
    st = {"t": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    errs, _ = state.validate([{"op": "deactivate_rows", "table": "t", "where": {"id": 1}}], st)
    assert any("is_active" in e for e in errs)


def test_data_op_unknown_column():
    st = {"t": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    errs, _ = state.validate([{"op": "insert_rows", "table": "t", "rows": [{"nope": 1}]}], st)
    assert any("nope" in e for e in errs)


def test_validate_does_not_mutate_input_state():
    st = {"t": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    state.validate([{"op": "drop_table", "table": "t", "reason": "test"}], st)
    assert "t" in st


# ---- SQL generation --------------------------------------------------------

def test_create_table_sql():
    res = run([create("customer", [col("customer_code", unique=True), col("name", nullable=False, comment="Legal name")])])
    sql_text = "\n".join(res.sql)
    assert 'CREATE TABLE "master"."customer"' in sql_text
    assert '"customer_id" integer GENERATED BY DEFAULT AS IDENTITY NOT NULL CONSTRAINT "customer_pkey" PRIMARY KEY' in sql_text
    assert '"created_at" timestamptz NOT NULL DEFAULT now()' in sql_text
    assert '"is_active" boolean NOT NULL DEFAULT true' in sql_text
    assert 'CONSTRAINT "customer_customer_code_key" UNIQUE' in sql_text
    assert "COMMENT ON COLUMN \"master\".\"customer\".\"name\" IS 'Legal name'" in sql_text


def test_identifiers_and_values_are_escaped():
    st = {"t": {"columns": {"id": {"type": "integer", "nullable": False}, "name": {"type": "text", "nullable": True}},
                "indexes": set(), "constraints": set()}}
    res = run([{"op": "insert_rows", "table": "t", "rows": [{"name": "O'Brien'); DROP TABLE t;--"}]}], st)
    text = res.sql[0]
    assert "'O''Brien''); DROP TABLE t;--'" in text   # quote doubled, stays inside one literal


def test_string_default_is_quoted_not_executed():
    res = run([create("t2", [col("tCode", unique=True), col("x", default="now(); DROP TABLE y")])])
    assert "DEFAULT 'now(); DROP TABLE y'" in "\n".join(res.sql)


def test_fk_is_named_and_qualified():
    st = {"customer": {"columns": {"id": {"type": "integer", "nullable": False}}, "indexes": set(), "constraints": set()}}
    res = run([{"op": "add_foreign_key", "table": "customer", "column": "id", "ref_table": "customer"}], st)
    assert 'ADD CONSTRAINT "fk_customer_id" FOREIGN KEY ("id") REFERENCES "master"."customer" ("id")' in res.sql[0]


def test_update_sets_updated_at_and_createdby_autofill():
    st = {"t": {"columns": {"id": {}, "name": {}, "updated_at": {}, "created_by": {}, "is_active": {}},
                "indexes": set(), "constraints": set()}}
    upd = run([{"op": "update_rows", "table": "t", "where": {"id": [1, 2]}, "set": {"name": "x"}}], st)
    assert '"updated_at" = now()' in upd.sql[0] and '"id" IN (1, 2)' in upd.sql[0]
    ins = run([{"op": "insert_rows", "table": "t", "rows": [{"name": "a"}]}], st, actor="me@example.com")
    assert '"created_by"' in ins.sql[0] and "'me@example.com'" in ins.sql[0] and "RETURNING" in ins.sql[0]
    deact = run([{"op": "deactivate_rows", "table": "t", "where": {"id": 3}}], st)
    assert '"is_active" = false' in deact.sql[0]


def test_alter_type_uses_cast():
    st = {"t": {"columns": {"n": {"type": "text", "nullable": True}}, "indexes": set(), "constraints": set()}}
    res = run([{"op": "alter_column_type", "table": "t", "column": "n", "type": "integer"}], st)
    assert 'TYPE integer USING "n"::integer' in res.sql[0]


def test_diff_lines():
    res = run([create()])
    assert res.diff[0].startswith("+ create table customer (customer_id integer")


def test_skip_rules_still_validates_semantics():
    res = run([{"op": "drop_table", "table": "ghost", "reason": "revert"}], skip_rules=True)
    assert res.errors and not res.ok
    ok = run([create("Bad_Name")], skip_rules=True)
    assert ok.ok and not ok.violations


# ---- <table>_id primary keys -------------------------------------------------

def test_foreign_key_defaults_to_the_target_tables_primary_key():
    st = {"province": {"columns": {"province_id": {"type": "integer", "nullable": False}},
                       "indexes": set(), "constraints": set(), "pk": "province_id"}}
    res = run([create("district", [col("district_code", unique=True),
                                   col("province_id", "integer", references={"table": "province"})])], st)
    assert res.ok, (res.errors, res.violations)
    assert 'REFERENCES "master"."province" ("province_id")' in "\n".join(res.sql)


def test_legacy_id_primary_key_is_still_understood():
    st = {"province": {"columns": {"id": {"type": "integer", "nullable": False}},
                       "indexes": set(), "constraints": set()}}
    res = run([{"op": "add_foreign_key", "table": "province", "column": "id", "ref_table": "province"}], st)
    assert 'REFERENCES "master"."province" ("id")' in res.sql[0]


def test_renaming_the_primary_key_keeps_track_of_it():
    st = {"province": {"columns": {"id": {"type": "integer", "nullable": False}},
                       "indexes": set(), "constraints": set(), "pk": "id"}}
    errs, after = state.validate([{"op": "rename_column", "table": "province", "column": "id",
                                   "new_name": "province_id"}], st)
    assert not errs and state.pk_of(after, "province") == "province_id"


# ---- the rules decide, not the code ------------------------------------------

def _with(rule_name, **param_changes):
    out = []
    for r in RULES:
        if r["name"] == rule_name:
            r = {**r, "params": {**r["params"], **param_changes}}
        out.append(r)
    return out


def test_renaming_an_audit_role_in_the_rule_changes_the_generated_sql():
    cols = [{**c, "name": "modified_on"} if c.get("role") == "updated_at" else c
            for c in RULES[2]["params"]["columns"]]
    assert RULES[2]["name"] == "auditColumns"
    rl = _with("auditColumns", columns=cols)
    st = {"t": {"columns": {"t_id": {}, "name": {}, "modified_on": {}, "is_active": {}}, "indexes": set(),
                "constraints": set(), "pk": "t_id"}}
    upd = run([{"op": "update_rows", "table": "t", "where": {"t_id": 1}, "set": {"name": "x"}}], st, rl=rl)
    assert '"modified_on" = now()' in upd.sql[0]


def test_kebab_case_is_just_a_custom_naming_rule():
    pattern = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
    rl = _with("tableNaming", style="custom", pattern=pattern)
    assert "tableNaming" not in names(run([create("customer-order")], rl=rl))
    assert "tableNaming" in names(run([create("customer_order")], rl=rl), "block")


def test_fk_must_be_named_like_the_primary_key_it_points_at():
    st = {"customer": {"columns": {"customer_id": {"type": "integer", "nullable": False}},
                       "indexes": set(), "constraints": set(), "pk": "customer_id"}}
    bad = run([create("invoice", [col("invoice_code", unique=True),
                                  col("buyer", "integer", references={"table": "customer"})])], st)
    v = next(v for v in bad.violations if v["rule"] == "foreignKeyNaming")
    assert v["suggestion"] == "customer_id"


def test_ident_and_type_regexes_reject_trailing_newline():
    from gate.operations import IDENT_RE, TYPE_RE, OpError, parse_ops
    assert IDENT_RE.match("x\n") is None and IDENT_RE.match("x")
    assert TYPE_RE.match("text\n") is None and TYPE_RE.match("text")
    with pytest.raises(OpError):
        parse_ops([{"op": "create_table", "name": "x\n", "columns": [{"name": "id", "type": "integer"}]}])
    with pytest.raises(OpError):
        parse_ops([{"op": "rename_column", "table": "t", "column": "c", "new_name": "d\n"}])
