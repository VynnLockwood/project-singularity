import pytest

from gate import check
from gate.defaults import DEFAULT_RULES
from schema import executor, introspect, query

pytestmark = pytest.mark.usefixtures("master")

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
ME = "me@example.com"


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def apply(ops, **kw):
    st = introspect.snapshot()
    res = check.run(ops, st, RULES, actor=ME, **kw)
    assert res.ok, (res.errors, res.violations)
    return executor.execute(res.operations, st, ME, rules=RULES)


def rows(sql):
    r = query.run_select(sql)
    names = [c["name"] if isinstance(c, dict) else c for c in r["columns"]]
    return [dict(zip(names, x)) for x in r["rows"]]


@pytest.fixture
def geo():
    apply([
        {"op": "create_table", "name": "province", "columns": [col("code", unique=True), col("name")]},
        {"op": "insert_rows", "table": "province", "rows": [{"code": "BKK", "name": "Bangkok"}, {"code": "CNX", "name": "Chiang Mai"}]},
        {"op": "create_table", "name": "district", "columns": [
            col("name"), col("population", "integer"), col("province_id", "integer", references={"table": "province"})]},
    ])


def bulk(expected=3, **kw):
    return {"op": "bulk_insert", "table": "district", "columns": ["name", "population", "province_code"],
            "source": {"script_id": 1, "stream": "district"},
            "refs": [{"column": "province_id", "parent_table": "province", "parent_key": "code",
                      "source_column": "province_code"}],
            "expected_rows": expected, **kw}


GOOD = [("Bang Rak", "50000", "BKK"), ("Suthep", "", "CNX"), ("Pathum Wan", "40000", "BKK")]


def load(data, op=None, **kw):
    st = introspect.snapshot()
    res = check.run([op or bulk()], st, RULES, actor=ME, allow_bulk=True)
    assert res.ok, (res.errors, res.violations)
    return executor.execute(res.operations, st, ME, rules=RULES, row_source=lambda o: iter(data), **kw)


def test_loads_rows_and_resolves_refs(geo):
    res = load(GOOD)
    got = rows("SELECT d.name, d.population, p.code, d.created_by FROM master.district d "
               "JOIN master.province p ON p.province_id = d.province_id ORDER BY d.district_id")
    assert [(r["name"], r["population"], r["code"], r["created_by"]) for r in got] == [
        ("Bang Rak", 50000, "BKK", ME), ("Suthep", None, "CNX", ME), ("Pathum Wan", 40000, "BKK", ME)]
    ids = [r["district_id"] for r in rows("SELECT district_id FROM master.district ORDER BY 1")]
    assert res.reverse_ops == [{"op": "deactivate_rows", "table": "district", "where": {"district_id": ids}}]


def test_orphan_ref_rolls_back(geo):
    bad = [*GOOD[:2], ("Nowhere", "1", "XXX")]
    with pytest.raises(executor.ApplyError, match="missing province.code"):
        load(bad)
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


def test_count_mismatch_rolls_back(geo):
    with pytest.raises(executor.ApplyError, match="expected 4"):
        load(GOOD, op=bulk(expected=4))
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


def test_bad_value_rolls_back(geo):
    with pytest.raises(executor.ApplyError):
        load([("A", "not-a-number", "BKK"), *GOOD[:2]])
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


def test_requires_row_source(geo):
    st = introspect.snapshot()
    res = check.run([bulk()], st, RULES, actor=ME, allow_bulk=True)
    with pytest.raises(executor.ApplyError, match="row source"):
        executor.execute(res.operations, st, ME, rules=RULES)


def test_gate_row_limit(geo):
    res = check.run([bulk(expected=200001)], introspect.snapshot(), RULES, actor=ME, allow_bulk=True)
    assert not res.ok and any("script row limit" in e for e in res.errors)
    # not capped by maxRowsPerDataOp
    res = check.run([bulk(expected=5000)], introspect.snapshot(), RULES, actor=ME, allow_bulk=True)
    assert res.ok, res.errors


def test_gate_refuses_bulk_by_default(geo):
    res = check.run([bulk()], introspect.snapshot(), RULES, actor=ME)
    assert not res.ok and any("script intake only" in e for e in res.errors)


def test_gate_state_checks(geo):
    st = introspect.snapshot()
    for op in (bulk(table="nope"), {**bulk(), "columns": ["name", "ghost", "province_code"]},
               {**bulk(), "refs": [{**bulk()["refs"][0], "parent_key": "ghost"}]},
               {**bulk(), "refs": [{**bulk()["refs"][0], "source_column": "zzz"}]}):
        assert not check.run([op], st, RULES, allow_bulk=True).ok


def test_sequence_advanced_after_load(geo):
    load(GOOD)
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "Next"}]}])
    assert rows("SELECT max(district_id) AS m, count(*) AS n FROM master.district")[0] == {"m": 4, "n": 4}


def test_explicit_pk_advances_sequence(geo):
    op = {**bulk(), "columns": ["district_id", "name", "population", "province_code"]}
    load([("100", "A", "1", "BKK"), ("101", "B", "2", "CNX"), ("102", "C", "3", "BKK")], op=op)
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "Next"}]}])
    assert rows("SELECT max(district_id) AS m FROM master.district")[0]["m"] == 103


def test_dry_run_rolls_back(geo):
    res = load(GOOD, dry_run=True)
    assert res.statements >= 1 and res.reverse_ops
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0
