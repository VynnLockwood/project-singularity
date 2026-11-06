import pytest

from gate import check, operations, state as gstate
from gate.defaults import DEFAULT_RULES
from schema import executor, introspect

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
ME = "me@example.com"


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


BASE = {
    "address": {
        "columns": {"address_id": {"type": "bigint", "nullable": False},
                    "postcode": {"type": "text", "nullable": True},
                    "city": {"type": "text", "nullable": True}},
        "indexes": set(), "constraints": {"address_pkey"}, "pk": "address_id",
    },
    "country": {
        "columns": {"country_id": {"type": "bigint", "nullable": False}},
        "indexes": set(), "constraints": set(), "pk": "country_id",
    },
}


def run(ops, st=BASE):
    return check.run(ops, st, RULES, skip_rules=True)


def add_check(expr, name="ck_address_postcode"):
    return {"op": "add_check", "table": "address", "name": name, "expression": expr}


# ---- add_check ---------------------------------------------------------------

def test_add_check_renders_and_tracks_constraint():
    res = run([add_check("postcode ~ '^[0-9]{5}$'")])
    assert res.ok, res.errors
    assert res.sql == [
        'ALTER TABLE "master"."address" ADD CONSTRAINT "ck_address_postcode" CHECK (postcode ~ \'^[0-9]{5}$\');'
    ]
    errors, after = gstate.validate(res.operations, BASE)
    assert "ck_address_postcode" in after["address"]["constraints"]
    assert "ck_address_postcode" not in BASE["address"]["constraints"]


@pytest.mark.parametrize("expr", [
    "(SELECT 1) = 1",
    "pg_sleep(1) IS NULL",
    "postcode IN (SELECT postcode FROM address)",
    "EXISTS (SELECT 1)",
    "length(postcode) = 5; DROP TABLE address",
    "length(postcode) = 5) , (1",
    "length(postcode) = 5 -- comment",
    "length(postcode) = 5 /* c */",
    "master.address.postcode = 'x'",
    "OPERATOR(pg_catalog.=)(postcode, 'x')",
    "postcode = $1",
    "sum(length(postcode)) > 1",
    "length(postcode) + 1",          # not boolean
    "postcode",                       # text, not boolean
    "",
    "postcode =",                     # syntax error
])
def test_add_check_rejects_unsafe_or_invalid_expressions(expr):
    res = run([add_check(expr)])
    assert res.errors, expr
    assert not res.ok


def test_add_check_unknown_column():
    res = run([add_check("nope > 1")])
    assert any("nope" in e for e in res.errors)


def test_add_check_unknown_table_and_duplicate_name():
    res = run([{"op": "add_check", "table": "ghost", "name": "c", "expression": "a > 1"}])
    assert any("ghost" in e for e in res.errors)
    res = run([add_check("length(postcode) = 5"), add_check("length(city) > 1")])
    assert any("already exists" in e for e in res.errors)


def test_add_check_allowed_functions_and_operators():
    for expr in [
        "char_length(postcode) = 5 AND city IS NOT NULL",
        "lower(city) = city OR upper(city) <> city",
        "coalesce(length(city), 0) >= 0",
        "abs(length(city)) < 100",
        "city IN ('a', 'b') AND postcode LIKE '1%' AND length(city) BETWEEN 1 AND 9",
        "NOT (city = 'x')",
        "postcode ~* 'abc'",
    ]:
        assert check_expr(expr) == [], expr


def check_expr(expr):
    return operations.check_expression(expr, {"postcode", "city"})


def test_check_expression_helper_returns_messages():
    assert operations.check_expression("(SELECT 1) = 1", {"a"})
    assert operations.check_expression("a > 1", {"a"}) == []


def test_add_check_shape():
    for bad in [
        {"op": "add_check", "table": "address", "name": "c"},
        {"op": "add_check", "table": "address", "name": "c", "expression": 5},
        {"op": "add_check", "table": "address", "name": "bad name", "expression": "a > 1"},
        {"op": "add_check", "table": "address", "name": "c", "expression": "a > 1", "x": 1},
    ]:
        with pytest.raises(operations.OpError):
            operations.parse_ops([bad])
    assert "add_check" in operations.SCHEMA_OPS and "set_table_comment" in operations.SCHEMA_OPS


# ---- set_table_comment ---------------------------------------------------------

def test_set_table_comment_renders():
    res = run([{"op": "set_table_comment", "table": "address", "comment": "Postal 'addresses'"}])
    assert res.ok, res.errors
    assert res.sql == ['COMMENT ON TABLE "master"."address" IS \'Postal \'\'addresses\'\'\';']


def test_set_table_comment_validation():
    with pytest.raises(operations.OpError):
        operations.parse_ops([{"op": "set_table_comment", "table": "address", "comment": 3}])
    res = run([{"op": "set_table_comment", "table": "ghost", "comment": "x"}])
    assert any("ghost" in e for e in res.errors)


# ---- on_delete ---------------------------------------------------------------------

@pytest.mark.parametrize("value", ["restrict", "cascade", "set null", "no action"])
def test_fk_on_delete_values(value):
    fk = {"op": "add_foreign_key", "table": "address", "column": "city", "ref_table": "country", "on_delete": value}
    res = run([fk])
    assert res.ok, res.errors
    assert res.sql[0].endswith(f"ON DELETE {value.upper()};")
    # default name unchanged
    assert '"fk_address_city"' in res.sql[0]


def test_fk_without_on_delete_has_no_clause():
    res = run([{"op": "add_foreign_key", "table": "address", "column": "city", "ref_table": "country"}])
    assert "ON DELETE" not in res.sql[0]


def test_fk_on_delete_bad_value():
    for bad in ["explode", "CASCADE; DROP", 5]:
        with pytest.raises(operations.OpError):
            operations.parse_ops([{"op": "add_foreign_key", "table": "address", "column": "city",
                                   "ref_table": "country", "on_delete": bad}])


def test_column_reference_on_delete():
    op = {"op": "create_table", "name": "person", "columns": [
        col("person_id", "bigint", primary_key=True),
        col("address_id", "bigint", references={"table": "address", "on_delete": "cascade"}),
    ]}
    res = run([op])
    assert res.ok, res.errors
    assert 'CONSTRAINT "fk_person_address_id" REFERENCES "master"."address" ("address_id") ON DELETE CASCADE' in res.sql[0]
    with pytest.raises(operations.OpError):
        operations.parse_ops([{**op, "columns": [col("address_id", references={"table": "address", "on_delete": "x"})]}])


# ---- live database: execution, reverse ops, snapshot ------------------------------

@pytest.mark.usefixtures("master")
def test_live_execute_reverse_and_snapshot():
    def apply(ops):
        st = introspect.snapshot()
        res = check.run(ops, st, RULES, actor=ME, skip_rules=True)
        assert res.ok, res.errors
        return executor.execute(res.operations, st, ME, rules=RULES)

    apply([{"op": "create_table", "name": "country", "comment": "Old comment",
            "columns": [col("country_id", "bigint", primary_key=True, identity=True)]},
           {"op": "create_table", "name": "address", "columns": [
               col("address_id", "bigint", primary_key=True, identity=True),
               col("postcode"), col("country_id", "bigint")]}])
    assert introspect.snapshot()["country"]["comment"] == "Old comment"

    r = apply([add_check("postcode ~ '^[0-9]{5}$'")])
    assert r.reverse_ops == [{"op": "drop_constraint", "table": "address", "name": "ck_address_postcode", "reason": "revert"}]
    assert "ck_address_postcode" in introspect.snapshot()["address"]["constraints"]
    d = introspect.describe_table("address")
    assert any(c["kind"] == "check" and c["name"] == "ck_address_postcode" for c in d["constraints"])

    r = apply([{"op": "set_table_comment", "table": "country", "comment": "New comment"}])
    assert r.reverse_ops == [{"op": "set_table_comment", "table": "country", "comment": "Old comment"}]
    assert introspect.snapshot()["country"]["comment"] == "New comment"
    apply(r.reverse_ops)
    assert introspect.snapshot()["country"]["comment"] == "Old comment"

    r = apply([{"op": "add_foreign_key", "table": "address", "column": "country_id", "ref_table": "country",
                "on_delete": "cascade"}])
    assert r.reverse_ops[0]["name"] == "fk_address_country_id"
    assert "ON DELETE CASCADE" in next(
        c["definition"] for c in introspect.describe_table("address")["constraints"] if c["name"] == "fk_address_country_id")


# ---- fix round 1 -------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "postcode SIMILAR TO '[0-9]{5}'",
    "postcode NOT SIMILAR TO '[0-9]+'",
    "postcode SIMILAR TO '[0-9]!%' ESCAPE '!'",
    "city LIKE 'a!%' ESCAPE '!'",
    "city ILIKE 'a!%' ESCAPE '!'",
    "city NOT LIKE 'a%'",
    "postcode::pg_catalog.text = 'x'",
    "(length(city) > 1)::bool",
])
def test_check_expression_accepts_similar_and_like_escape(expr):
    assert check_expr(expr) == [], expr
    res = run([add_check(expr)])
    assert res.ok, res.errors


@pytest.mark.parametrize("expr", [
    "city COLLATE \"C\" = 'x'",
    "city = ANY(ARRAY['a','b'])",
    "ROW(city, postcode) = ROW('a','b')",
    "postcode::master.text = 'x'",
    "city::pg_catalog.pg_class = 'x'",
    "length(city) > 1) AS z, (1",
    "like_escape(city, 'x') = 'y'",
    "pg_catalog.similar_to_escape(city) = 'y'",
    "city = pg_catalog.like_escape('a', 'b')",
    "city LIKE pg_catalog.similar_to_escape('a')",
    "city SIMILAR TO pg_catalog.like_escape('a', 'b')",
    "city LIKE (SELECT 'a')",
    "city LIKE 'a' ESCAPE (SELECT '!')",
    "city",
])
def test_check_expression_rejects_more_forms(expr):
    assert check_expr(expr), expr


def test_sqlgen_never_renders_unvalidated_expression():
    from gate import sqlgen
    op = add_check("(SELECT 1) = 1")
    with pytest.raises(sqlgen.UnsafeOp):
        sqlgen.statements_for(op, BASE, None)
    with pytest.raises(sqlgen.UnsafeOp):
        sqlgen.statements_for(add_check("nope > 1"), BASE, None)
    # and check.run turns a late refusal into a gate error rather than raising
    from unittest import mock
    with mock.patch.object(sqlgen, "_render_check", side_effect=sqlgen.UnsafeOp("boom")):
        res = run([add_check("length(city) > 1")])
    assert res.errors == ["boom"] and not res.ok


# ---- fix round 2 -------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "city LIKE pg_catalog.like_escape('a','!') FILTER (WHERE (SELECT 1)=1)",
    "city LIKE like_escape(DISTINCT 'a','!')",
    "city LIKE pg_catalog.like_escape('a','!') OVER ()",
    "city LIKE pg_catalog.like_escape(VARIADIC ARRAY['a'])",
    "city SIMILAR TO pg_catalog.similar_to_escape('a') FILTER (WHERE true)",
    "city::text((SELECT 1)) = 'x'",
    "city::varchar(length(city)) = 'x'",
])
def test_round2_rejections(expr):
    assert check_expr(expr), expr


def test_round2_plain_typmods_still_ok():
    assert check_expr("city::varchar(5) = 'x'") == []
    assert check_expr("length(city)::numeric(5,2) > 1") == []
