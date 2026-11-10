"""Raw SQL: parse and refuse (plan C task 2). Pure: no database."""
import pytest

from approvals.rawsql import RawSqlError, Statement, parse_statements

LIMITS = dict(max_bytes=100000, max_statements=50)


def parse(sql, **kw):
    return parse_statements(sql, **{**LIMITS, **kw})


def test_two_statements_are_split_and_numbered():
    got = parse("UPDATE t SET a = 1;\nDELETE FROM t WHERE a = 2;")
    assert [s.text for s in got] == ["UPDATE t SET a = 1", "DELETE FROM t WHERE a = 2"]
    assert [s.number for s in got] == [1, 2]
    assert all(isinstance(s, Statement) for s in got)


def test_non_ascii_text_is_sliced_correctly():
    got = parse("UPDATE t SET a = 'héllo'; UPDATE t SET a = 'ü'")
    assert [s.text for s in got] == ["UPDATE t SET a = 'héllo'", "UPDATE t SET a = 'ü'"]


@pytest.mark.parametrize("sql", ["", "   \n ", "-- only a comment", ";;"])
def test_nothing_to_run_is_refused(sql):
    with pytest.raises(RawSqlError, match="no statements"):
        parse(sql)


def test_syntax_error_says_where():
    with pytest.raises(RawSqlError, match="syntax error.*position"):
        parse("SELECT 1; selec 2")


def test_size_and_statement_caps_name_their_setting():
    with pytest.raises(RawSqlError, match="limits.maxSqlBytes"):
        parse("SELECT 1; " * 10, max_bytes=20)
    with pytest.raises(RawSqlError, match="limits.maxSqlStatements"):
        parse("SELECT 1; SELECT 2; SELECT 3", max_statements=2)


REFUSED = [
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT a", "PREPARE TRANSACTION 'x'",
    "SET ROLE postgres", "SET SESSION AUTHORIZATION x", "RESET ROLE",
    "SELECT set_config('role', 'postgres', false)",
    "SELECT 1 WHERE (SELECT set_config('role', 'x', false)) = 'x'",
    "COPY t TO PROGRAM 'ls'", "COPY t FROM '/etc/passwd'", "COPY t TO '/tmp/x'",
    "DO $$ BEGIN END $$ LANGUAGE plperl",
    "CREATE FUNCTION f() RETURNS int LANGUAGE c AS 'x'",
    "VACUUM t", "CREATE DATABASE d", "DROP DATABASE d", "ALTER SYSTEM SET work_mem = 1",
    "LISTEN c", "UNLISTEN c", "NOTIFY c", "LOAD 'x'",
    "CREATE SCHEMA s", "DROP SCHEMA s", "ALTER SCHEMA s RENAME TO t", "ALTER SCHEMA s OWNER TO x",
    "CREATE EXTENSION hstore", "CREATE ROLE r", "ALTER ROLE r SUPERUSER", "DROP ROLE r", "GRANT r TO s",
    "CREATE INDEX CONCURRENTLY i ON t (a)", "DROP INDEX CONCURRENTLY i", "REINDEX INDEX CONCURRENTLY i",
]


@pytest.mark.parametrize("sql", REFUSED)
def test_refused_statements_name_the_statement(sql):
    with pytest.raises(RawSqlError) as e:
        parse("SELECT 1; " + sql)
    assert e.value.message.startswith("statement 2:") and "not allowed" in e.value.message or "CONCURRENTLY" in e.value.message


ALLOWED = [
    "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'select 1'",
    "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    "CREATE TRIGGER g BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f()",
    "DO $$ BEGIN END $$", "GRANT SELECT ON t TO reporting", "ANALYZE t",
    "UPDATE t SET a = 1", "ALTER TABLE t ADD COLUMN c int", "SELECT 1",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_statements_pass(sql):
    assert parse(sql)[0].text == sql


# --- final review fixes: identity/timeout escapes, role defaults, singularity internals ---

@pytest.mark.parametrize("sql", [
    'SET "ROLE" = \'postgres\'',
    "SET LOCAL statement_timeout = 0",
    "SET lock_timeout TO 0",
    "SET idle_in_transaction_session_timeout = 0",
    "SET search_path = crm",
    "SET default_transaction_read_only = off",
    "RESET ALL",
    "RESET statement_timeout",
    "RESET ROLE",
])
def test_settings_that_escape_the_fence_are_refused(sql):
    with pytest.raises(RawSqlError, match="statement 1"):
        parse(sql)


@pytest.mark.parametrize("sql", [
    "ALTER ROLE singularity_master_rw SET default_transaction_read_only = on",
    "ALTER DATABASE singularity SET search_path = x",
    "DROP OWNED BY singularity_master_rw",
    "REASSIGN OWNED BY singularity_master_rw TO postgres",
    "REVOKE SELECT ON ALL TABLES IN SCHEMA master FROM singularity_master_ro",
    "GRANT SELECT ON t TO singularity_master_ro",
    "SELECT singularity.create_managed_schema('evil')",
    "SELECT pg_notify('c', 'x')",
    "SELECT pg_catalog.set_config('role', 'x', false)",
])
def test_role_defaults_ownership_and_internals_are_refused(sql):
    with pytest.raises(RawSqlError, match="statement 1"):
        parse(sql)


@pytest.mark.parametrize("body", [
    "BEGIN EXECUTE 'SET ROLE postgres'; END",
    "BEGIN PERFORM set_config('role', 'x', false); END",
    "BEGIN EXECUTE 'SET SESSION AUTHORIZATION postgres'; END",
    "BEGIN EXECUTE 'SET statement_timeout = 0'; END",
    "BEGIN EXECUTE 'NOTIFY c'; END",
    "BEGIN PERFORM singularity.rename_managed_schema('a', 'b'); END",
])
def test_blocks_that_try_the_same_things_are_refused(body):
    with pytest.raises(RawSqlError, match="statement 1"):
        parse(f"DO $$ {body} $$")
    with pytest.raises(RawSqlError, match="statement 1"):
        parse(f"CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ {body} $$")


def test_function_level_role_setting_is_refused():
    with pytest.raises(RawSqlError, match="statement 1"):
        parse("CREATE FUNCTION f() RETURNS void LANGUAGE sql SET role = 'postgres' AS 'SELECT 1'")


def test_ordinary_blocks_and_grants_still_pass():
    parse("DO $$ BEGIN UPDATE t SET a = 1; END $$; GRANT SELECT ON t TO reporting; SET LOCAL work_mem = '64MB'")
