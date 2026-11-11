"""Raw SQL: warnings and the dry run (plan C task 3). Real Postgres."""
import pytest

from approvals import rawsql
from approvals.rawsql import RawSqlError, parse_statements
from schema import executor, introspect
from schema.conn import rw_connection

from .ops_helpers import apply, col, make_table, rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


def stmts(sql):
    return parse_statements(sql, max_bytes=100000, max_statements=50)


def test_dry_run_reports_tags_and_changes_nothing():
    make_table(rows=[{"a": "1"}, {"a": "2"}, {"a": "3"}])
    lines = rawsql.dry_run(stmts("ALTER TABLE t ADD COLUMN c int; UPDATE t SET a = 'x'"), "master")
    assert lines[0].startswith("ALTER TABLE") and "(statement 1)" in lines[0]
    assert lines[1].startswith("UPDATE 3") and "(statement 2)" in lines[1]
    assert "c" not in introspect.snapshot("master")["t"]["columns"]
    assert sorted(r[0] for r in rows("SELECT a FROM master.t")) == ["1", "2", "3"]


def test_dry_run_error_names_the_statement():
    make_table()
    with pytest.raises(RawSqlError, match=r"statement 2.*nope"):
        rawsql.dry_run(stmts("SELECT 1; SELECT * FROM nope"), "master")


def test_dry_run_does_not_move_sequences():
    make_table(rows=[{"a": "1"}, {"a": "2"}, {"a": "3"}])
    for _ in range(2):
        rawsql.dry_run(stmts("INSERT INTO t (a) VALUES ('q')"), "master")
    apply([{"op": "insert_rows", "table": "t", "rows": [{"a": "z"}]}])
    assert rows("SELECT max(t_id) FROM master.t")[0][0] == 4


@pytest.fixture
def crm():
    with rw_connection(autocommit=True) as rw:
        rw.execute("SELECT singularity.create_managed_schema('crm')")
    yield
    with rw_connection(autocommit=True) as rw:
        rw.execute("DROP SCHEMA IF EXISTS crm CASCADE")


def test_dry_run_runs_in_the_chosen_schema(crm):
    apply([{"op": "create_table", "name": "t", "columns": [col("t_id", "integer", primary_key=True, identity=True), col("a")]}], schema="crm")
    lines = rawsql.dry_run(stmts("CREATE TABLE z (a int); SELECT * FROM t"), "crm")
    assert len(lines) == 2
    assert "z" not in introspect.snapshot("crm")


def test_statement_timeout_is_an_error_not_a_hang(monkeypatch):
    monkeypatch.setattr(executor, "STATEMENT_TIMEOUT", "200ms")
    with pytest.raises(RawSqlError, match="statement 1"):
        rawsql.dry_run(stmts("SELECT pg_sleep(2)"), "master")


def warn(sql, schema="master"):
    return [w["message"] for w in rawsql.warnings_for(stmts(sql), schema)]


def test_drop_truncate_and_unfiltered_delete_warn():
    assert any("statement 1" in m and "drop" in m.lower() for m in warn("DROP TABLE master.t"))
    assert any("truncate" in m.lower() for m in warn("TRUNCATE master.t"))
    assert any("without a WHERE" in m for m in warn("DELETE FROM master.t"))
    assert not any("WHERE" in m for m in warn("DELETE FROM master.t WHERE a = 'x'"))


def test_unqualified_and_foreign_schema_targets_warn():
    assert any("without a schema" in m and "resolves in master" in m for m in warn("UPDATE t SET a = 1"))
    assert not warn("UPDATE master.t SET a = 1")
    assert any("touches schema other, not the chosen database master" in m for m in warn("UPDATE other.t SET a = 1"))
    assert len([m for m in warn("UPDATE t SET a = (SELECT max(a) FROM t)") if "without a schema" in m]) == 1


def test_a_cte_name_is_not_a_table():
    assert not warn("WITH x AS (SELECT 1 AS a FROM master.t) SELECT * FROM x")


def test_schema_fingerprint_changes_with_a_view_and_with_a_column():
    make_table()
    f0 = introspect.schema_fingerprint("master")
    assert introspect.schema_fingerprint("master") == f0
    apply([{"op": "create_view", "name": "v", "query": "SELECT a FROM t"}])
    f1 = introspect.schema_fingerprint("master")
    assert f1 != f0
    apply([{"op": "add_column", "table": "t", "column": col("b")}])
    assert introspect.schema_fingerprint("master") not in (f0, f1)


def _seq_state(name):
    return rows(f"SELECT last_value, is_called FROM {name}")[0]


def test_dry_run_does_not_move_a_sequence_of_another_schema(crm):
    apply([{"op": "create_table", "name": "c", "columns": [col("c_id", "integer", primary_key=True, identity=True), col("a")]}], schema="crm")
    apply([{"op": "insert_rows", "table": "c", "rows": [{"a": "1"}, {"a": "2"}]}], schema="crm")
    before = _seq_state("crm.c_c_id_seq")
    rawsql.dry_run(stmts("INSERT INTO crm.c (a) VALUES ('x'), ('y'), ('z')"), "master")
    assert _seq_state("crm.c_c_id_seq") == before
    rawsql.dry_run(stmts("SELECT setval(pg_get_serial_sequence('crm.c', 'c_id'), 1, false)"), "master")
    assert _seq_state("crm.c_c_id_seq") == before


def test_dry_run_does_not_move_a_sequence_no_table_owns():
    make_table()
    with rw_connection(autocommit=True) as rw:
        rw.execute("CREATE SEQUENCE master.loose")
    rawsql.dry_run(stmts("SELECT nextval('loose'), nextval('loose')"), "master")
    assert _seq_state("master.loose")[1] is False


@pytest.mark.parametrize("ddl", [
    "CREATE FUNCTION master.f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "CREATE SEQUENCE master.s2",
    "CREATE DOMAIN master.d AS text",
    "CREATE MATERIALIZED VIEW master.mv AS SELECT 1 AS x",
    "ALTER TABLE master.t ENABLE ROW LEVEL SECURITY",
    "CREATE RULE r AS ON INSERT TO master.t DO ALSO NOTHING",
    "GRANT SELECT ON master.t TO PUBLIC",
    "CREATE TRIGGER trg BEFORE INSERT ON master.t FOR EACH ROW EXECUTE FUNCTION master.tf()",
    "CREATE OR REPLACE FUNCTION master.tf() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NULL; END'",
    "CREATE OR REPLACE VIEW master.v AS SELECT a, 2 AS two FROM master.t",
])
def test_schema_fingerprint_sees_what_raw_sql_can_change(ddl):
    make_table()
    with rw_connection(autocommit=True) as rw:
        rw.execute("CREATE FUNCTION master.tf() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'")
        rw.execute("CREATE VIEW master.v AS SELECT a FROM master.t")
    before = introspect.schema_fingerprint("master")
    with rw_connection(autocommit=True) as rw:
        rw.execute(ddl.replace("CREATE RULE r", "CREATE RULE r"))
    assert introspect.schema_fingerprint("master") != before


def test_schema_fingerprint_ignores_data_and_sequence_position():
    make_table(rows=[{"a": "1"}])
    before = introspect.schema_fingerprint("master")
    apply([{"op": "insert_rows", "table": "t", "rows": [{"a": "2"}]}])
    assert introspect.schema_fingerprint("master") == before


def test_dynamic_sql_in_a_block_gets_a_warning_that_it_cannot_be_checked():
    got = rawsql.warnings_for(stmts("DO $$ BEGIN EXECUTE 'DROP SCHEMA crm CASCADE'; END $$"), "master")
    assert any("dynamic SQL" in w["message"] and "statement 1" in w["message"] for w in got)
    assert not rawsql.warnings_for(stmts("DO $$ BEGIN UPDATE t SET a = 1; END $$"), "master")
