"""Task 2: harmless or non-gateable statements are skipped and reported, not failed."""
import pytest

from scripts.sqlparse import parse_script
from tests.bigdump import PROBE

T = "CREATE TABLE master.a (id integer NOT NULL, n text);\n"


def test_probe_parses_with_no_errors():
    p = parse_script(PROBE)
    assert p.errors == []
    assert [o["op"] for o in p.ops].count("create_table") == 1
    assert list(p.streams) == ["province"]


@pytest.mark.parametrize("sql,kind", [
    ("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;", "extension"),
    ("COMMENT ON EXTENSION pg_trgm IS 'x';", "extension"),
    ("CREATE SEQUENCE master.s START 1;", "sequence"),
    ("ALTER SEQUENCE master.s OWNED BY master.a.id;", "sequence"),
    ("GRANT ALL ON TABLE master.a TO app;", "permission"),
    ("REVOKE ALL ON TABLE master.a FROM app;", "permission"),
    ("GRANT app TO bob;", "permission"),
    ("ALTER DEFAULT PRIVILEGES IN SCHEMA master GRANT SELECT ON TABLES TO app;", "permission"),
    ("CREATE FUNCTION master.f() RETURNS integer LANGUAGE sql AS $$ SELECT 1; SELECT 2 $$;", "logic"),
    ("CREATE TRIGGER t BEFORE INSERT ON master.a FOR EACH ROW EXECUTE FUNCTION master.f();", "logic"),
    ("CREATE POLICY p ON master.a USING (true);", "logic"),
    ("CREATE RULE r AS ON INSERT TO master.a DO NOTHING;", "logic"),
])
def test_skip_kinds(sql, kind):
    p = parse_script(T + sql + "\n")
    assert p.errors == []
    assert len(p.skipped) == 1
    s = p.skipped[0]
    assert s["kind"] == kind and s["line"] == 2 and s["reason"] and s["statement"]
    assert len(p.ops) == 1


def test_setval_stays_ignored_not_skipped():
    p = parse_script(T + "SELECT pg_catalog.setval('master.s', 10, true);\n")
    assert p.errors == [] and p.skipped == [] and len(p.ignored) >= 1


def test_nextval_default_dropped_in_create_table():
    p = parse_script("CREATE TABLE master.a (id integer DEFAULT nextval('master.s'::regclass) NOT NULL, "
                     "n integer DEFAULT nextval('master.s2'));\n")
    assert p.errors == []
    cols = p.ops[0]["columns"]
    assert all("default" not in c for c in cols)
    assert [s["kind"] for s in p.skipped] == ["default", "default"]


def test_nextval_default_dropped_via_alter():
    p = parse_script(T + "ALTER TABLE ONLY master.a ALTER COLUMN id SET DEFAULT nextval('master.s'::regclass);\n")
    assert p.errors == []
    assert "default" not in p.ops[0]["columns"][0]
    assert [s["kind"] for s in p.skipped] == ["default"]


@pytest.mark.parametrize("sql", [
    "DROP TABLE master.a;", "DELETE FROM master.a;", "UPDATE master.a SET n = 'x';",
    "TRUNCATE master.a;", "ALTER TABLE master.a DROP COLUMN n;",
])
def test_destructive_statements_still_error(sql):
    p = parse_script(T + "GRANT ALL ON TABLE master.a TO app;\n" + sql + "\n")
    assert len(p.errors) == 1 and p.errors[0].line == 3
    assert len(p.skipped) == 1


def test_function_body_skipped_whole():
    p = parse_script("CREATE FUNCTION master.f() RETURNS integer LANGUAGE sql AS $$ SELECT 1; SELECT 2 $$;\n" + T)
    assert p.errors == [] and [o["op"] for o in p.ops] == ["create_table"] and len(p.skipped) == 1


@pytest.mark.parametrize("sql", [                                       # review finding 3
    "COMMENT ON FUNCTION master.f() IS 'x';",
    "COMMENT ON SEQUENCE master.s IS 'x';",
    "COMMENT ON TRIGGER t ON master.a IS 'x';",
    "ALTER TABLE master.a ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE master.a FORCE ROW LEVEL SECURITY;",
    "ALTER TABLE master.a DISABLE TRIGGER ALL;",
])
def test_statements_that_go_with_skipped_objects_are_skipped(sql):
    p = parse_script(T + sql + "\n")
    assert p.errors == [] and len(p.skipped) == 1 and len(p.ops) == 1
