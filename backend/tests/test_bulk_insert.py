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
    op = {"op": "bulk_insert", "table": "district", "columns": ["name", "population", "province_code"],
          "refs": [{"column": "province_id", "parent_table": "province", "parent_key": "code",
                    "source_column": "province_code"}],
          "expected_rows": expected, **kw}
    # stream columns named like the op columns (one per op column)
    op.setdefault("source", {"script_id": 1, "stream": "district", "columns": list(op["columns"])})
    return op


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
    for op in (bulk(table="nope"), bulk(columns=["name", "ghost", "province_code"]),
               {**bulk(), "refs": [{**bulk()["refs"][0], "parent_key": "ghost"}]},
               {**bulk(), "refs": [{**bulk()["refs"][0], "source_column": "zzz"}]}):
        assert not check.run([op], st, RULES, allow_bulk=True).ok


def test_sequence_advanced_after_load(geo):
    load(GOOD)
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "Next"}]}])
    assert rows("SELECT max(district_id) AS m, count(*) AS n FROM master.district")[0] == {"m": 4, "n": 4}


def test_explicit_pk_advances_sequence(geo):
    op = bulk(columns=["district_id", "name", "population", "province_code"])
    load([("100", "A", "1", "BKK"), ("101", "B", "2", "CNX"), ("102", "C", "3", "BKK")], op=op)
    apply([{"op": "insert_rows", "table": "district", "rows": [{"name": "Next"}]}])
    assert rows("SELECT max(district_id) AS m FROM master.district")[0]["m"] == 103


def test_dry_run_rolls_back(geo):
    res = load(GOOD, dry_run=True)
    assert res.statements >= 1 and res.reverse_ops
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


# ---- fix round 1 -----------------------------------------------------------

def seq_state():
    from schema.conn import rw_connection
    with rw_connection() as c:
        return c.execute("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'master' "
                         "ORDER BY 1").fetchall()


def test_dry_run_restores_sequences(geo):
    before = seq_state()
    load(GOOD, dry_run=True)
    assert seq_state() == before
    op = bulk(columns=["district_id", "name", "population", "province_code"])
    load([("100", "A", "1", "BKK"), ("101", "B", "2", "CNX"), ("102", "C", "3", "BKK")], op=op, dry_run=True)
    assert seq_state() == before
    load(GOOD)
    assert rows("SELECT max(district_id) AS m FROM master.district")[0]["m"] == 3


def test_dry_run_restores_never_called_sequence(geo):
    from schema.conn import rw_connection
    with rw_connection() as c:
        c.execute("TRUNCATE master.district RESTART IDENTITY")
    before = seq_state()
    assert [r["last_value"] for r in before if r["sequencename"].startswith("district")] == [None]
    load(GOOD, dry_run=True)
    assert seq_state() == before
    load(GOOD)
    assert rows("SELECT min(district_id) AS m FROM master.district")[0]["m"] == 1


def test_too_many_rows_stops_early(geo):
    consumed = []

    def gen():
        for i in range(10**6):
            consumed.append(i)
            yield ("x", "1", "BKK")
    st = introspect.snapshot()
    res = check.run([bulk()], st, RULES, actor=ME, allow_bulk=True)
    with pytest.raises(executor.ApplyError, match="more than"):
        executor.execute(res.operations, st, ME, rules=RULES, row_source=lambda o: gen())
    assert len(consumed) == 4


def test_reserved_column_name(geo):
    res = check.run([bulk(columns=["__row", "province_code"])], introspect.snapshot(), RULES, allow_bulk=True)
    assert not res.ok and any("reserved" in e for e in res.errors)


def test_two_bulk_ops_in_one_execute(geo):
    st = introspect.snapshot()
    res = check.run([bulk(), bulk(2)], st, RULES, actor=ME, allow_bulk=True)
    assert res.ok, res.errors
    r = executor.execute(res.operations, st, ME, rules=RULES, row_source=lambda o: iter(GOOD[:o["expected_rows"]]))
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 5
    assert len(r.reverse_ops) == 2


def test_empty_string_becomes_null_for_non_text(geo):
    apply([{"op": "add_column", "table": "district", "column": col("extra", "jsonb")}])
    op = bulk(columns=["name", "extra", "population", "province_code"])
    load([("", "", "", "BKK")], op=bulk(1, **{"columns": op["columns"]}))
    r = rows("SELECT name, extra, population FROM master.district")[0]
    assert r == {"name": "", "extra": None, "population": None}


def test_cast_treats_char_like_text():
    from psycopg import sql
    for t in ("text", "character varying(5)", "character(3)", "bpchar"):
        assert "NULLIF" not in executor._cast(sql.Identifier("c"), t).as_string(None)
    assert "NULLIF" in executor._cast(sql.Identifier("c"), "integer").as_string(None)


def test_copy_escaping(geo):
    nasty = ["tab\there", "line\nbreak", "back\\slash", "\\N", "ภาษาไทย", 'quote"s', "a,b"]
    op = bulk(len(nasty), **{"columns": ["name", "province_code"]})
    load([(n, "BKK") for n in nasty], op=op)
    got = [r["name"] for r in rows("SELECT name FROM master.district ORDER BY district_id")]
    assert got == nasty


def test_null_source_value(geo):
    load([(None, None, None)], op=bulk(1))
    r = rows("SELECT name, province_id FROM master.district")[0]
    assert r == {"name": None, "province_id": None}


def test_duplicate_parent_key_is_an_error(geo):
    from schema.conn import rw_connection
    with rw_connection() as c:
        c.execute("ALTER TABLE master.province DROP CONSTRAINT province_code_key")
        c.execute("INSERT INTO master.province (code, name) VALUES ('BKK', 'Dup')")
    with pytest.raises(executor.ApplyError, match="inserted"):
        load(GOOD)
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


def test_table_without_pk_and_without_is_active_is_irreversible():
    from schema.conn import rw_connection
    with rw_connection() as c:
        c.execute("CREATE TABLE master.plain (name text, qty integer)")
    op = {"op": "bulk_insert", "table": "plain", "columns": ["name", "qty"], "source": {"script_id": 1, "stream": "s", "columns": ["name", "qty"]},
          "refs": [], "expected_rows": 2}
    st = introspect.snapshot()
    res = check.run([op], st, RULES, actor=ME, allow_bulk=True)
    assert res.ok, res.errors
    r = executor.execute(res.operations, st, ME, rules=RULES, row_source=lambda o: iter([("a", "1"), ("b", "2")]))
    assert r.reverse_ops is None
    assert rows("SELECT count(*) AS n FROM master.plain")[0]["n"] == 2


def test_no_is_active_role_gives_none_reverse():
    from schema.conn import rw_connection
    with rw_connection() as c:
        c.execute("CREATE TABLE master.thing (thing_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name text)")
    op = {"op": "bulk_insert", "table": "thing", "columns": ["name"], "source": {"script_id": 1, "stream": "s", "columns": ["name"]},
          "refs": [], "expected_rows": 1}
    st = introspect.snapshot()
    res = check.run([op], st, RULES, actor=ME, allow_bulk=True)
    assert res.ok, res.errors
    r = executor.execute(res.operations, st, ME, rules=RULES, row_source=lambda o: iter([("a",)]))
    assert r.reverse_ops is None


# ---- fix round 2: dry-run locking and sequence scope ----------------------

def test_dry_run_restores_sequences_when_a_later_op_fails(geo):
    before = seq_state()
    st = introspect.snapshot()
    res = check.run([bulk(), bulk()], st, RULES, actor=ME, allow_bulk=True)
    assert res.ok, res.errors
    calls = []

    def source(op):
        calls.append(1)
        return iter(GOOD if len(calls) == 1 else [*GOOD[:2], ("Nowhere", "1", "XXX")])
    with pytest.raises(executor.ApplyError, match="missing province.code"):
        executor.execute(res.operations, st, ME, rules=RULES, row_source=source, dry_run=True)
    assert len(calls) == 2
    assert seq_state() == before
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


def test_dry_run_fails_when_a_writer_holds_the_table(geo, monkeypatch):
    from schema.conn import rw_connection
    monkeypatch.setattr(executor, "LOCK_TIMEOUT", "300ms")
    with rw_connection() as writer:
        writer.execute("INSERT INTO master.district (name) VALUES ('busy')")  # open transaction
        with pytest.raises(executor.ApplyError, match="lock"):
            load(GOOD, dry_run=True)
        writer.rollback()
    assert rows("SELECT count(*) AS n FROM master.district")[0]["n"] == 0


def test_dry_run_only_looks_at_sequences_of_targeted_tables(geo):
    from schema.conn import rw_connection
    with rw_connection() as c:
        names = [s["name"] for s in executor._sequence_states(c, ["district"], "master")]
        assert len(names) == 1 and names[0].startswith("master.district")
        assert executor._sequence_states(c, [], "master") == []
    assert executor._insert_targets([bulk(), {"op": "update_rows", "table": "province", "where": {"code": "x"}, "set": {}}],
                                    introspect.snapshot(), "master") == ["district"]
