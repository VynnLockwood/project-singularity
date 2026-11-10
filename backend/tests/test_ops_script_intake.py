"""Script intake for column defaults, not-null, views and enums (plan B task 9)."""
import pytest

from accounts.models import Role, User
from approvals import service as approvals
from schema import introspect, query
from scripts import service
from scripts.mapping import apply_mapping, suggest_mapping
from scripts.sqlparse import parse_script

from .test_script_sqlparse import col, ops_of, table


def ops(text):
    p = parse_script(text)
    assert p.errors == [], p.errors
    return p.ops


def errors(text):
    return [(e.line, e.message) for e in parse_script(text).errors]


# ---------------------------------------------------------------- column ops on tables outside the script

@pytest.mark.parametrize("sql,expected", [
    ("ALTER TABLE ONLY t ALTER COLUMN c SET DEFAULT 'x';", {"op": "set_column_default", "table": "t", "column": "c", "default": "x"}),
    ("ALTER TABLE t ALTER COLUMN c SET DEFAULT 5;", {"op": "set_column_default", "table": "t", "column": "c", "default": 5}),
    ("ALTER TABLE t ALTER COLUMN c SET DEFAULT now();", {"op": "set_column_default", "table": "t", "column": "c", "default": "now()"}),
    ("ALTER TABLE t ALTER COLUMN c DROP DEFAULT;", {"op": "drop_column_default", "table": "t", "column": "c"}),
    ("ALTER TABLE t ALTER COLUMN c SET NOT NULL;", {"op": "set_not_null", "table": "t", "column": "c"}),
    ("ALTER TABLE t ALTER COLUMN c DROP NOT NULL;", {"op": "drop_not_null", "table": "t", "column": "c"}),
])
def test_alter_column_statements_become_ops(sql, expected):
    assert ops(sql) == [expected]


def test_alter_column_on_a_table_made_in_the_script_folds_into_the_column():
    p = parse_script("CREATE TABLE t (id int, c text, d text NOT NULL DEFAULT 'a');\n"
                     "ALTER TABLE t ALTER COLUMN c SET NOT NULL;\n"
                     "ALTER TABLE t ALTER COLUMN c SET DEFAULT 'z';\n"
                     "ALTER TABLE t ALTER COLUMN d DROP NOT NULL;\n"
                     "ALTER TABLE t ALTER COLUMN d DROP DEFAULT;\n")
    assert p.errors == []
    t = table(p, "t")
    assert col(t, "c")["nullable"] is False and col(t, "c")["default"] == "z"
    assert col(t, "d").get("nullable", True) is True and "default" not in col(t, "d")
    assert [o["op"] for o in p.ops] == ["create_table"]


def test_a_default_that_is_a_function_outside_the_allowed_ones_is_refused_with_its_line():
    errs = errors("SELECT 1 WHERE false;\n".replace("SELECT 1 WHERE false;", "SET client_encoding = 'UTF8';")
                  + "ALTER TABLE t ALTER COLUMN c SET DEFAULT random();\n")
    assert len(errs) == 1 and errs[0][0] == 2 and "default" in errs[0][1]


# ---------------------------------------------------------------- views

def test_create_view_becomes_create_view_with_unqualified_names():
    got = ops("CREATE TABLE public.t (a int);\nCREATE VIEW public.v AS SELECT a FROM public.t WHERE a > 1;")
    v = ops_of(parse_script("CREATE VIEW v AS SELECT a FROM t;"), "create_view")[0]
    assert v == {"op": "create_view", "name": "v", "query": "SELECT a FROM t"}
    assert got[-1]["op"] == "create_view" and "public" not in got[-1]["query"]


def test_drop_view_gets_the_reason_script():
    assert ops("DROP VIEW v;") == [{"op": "drop_view", "name": "v", "reason": "script"}]


@pytest.mark.parametrize("sql,word", [
    ("CREATE OR REPLACE VIEW v AS SELECT 1 AS a;", "REPLACE"),
    ("CREATE VIEW v (x) AS SELECT 1 AS a;", "column"),
    ("CREATE VIEW v AS SELECT 1 AS a WITH CHECK OPTION;", "CHECK OPTION"),
    ("CREATE VIEW v WITH (security_barrier) AS SELECT 1 AS a;", "options"),
    ("CREATE TEMP VIEW v AS SELECT 1 AS a;", "temporary"),
    ("DROP VIEW IF EXISTS v;", "IF EXISTS"),
    ("DROP VIEW v CASCADE;", "CASCADE"),
])
def test_unsupported_view_variants_are_refused_with_the_line(sql, word):
    errs = errors("SET client_encoding = 'UTF8';\n" + sql + "\n")
    assert len(errs) == 1 and errs[0][0] == 2 and word.lower() in errs[0][1].lower(), errs


# ---------------------------------------------------------------- enums

def test_create_type_enum_and_add_value():
    assert ops("CREATE TYPE public.status AS ENUM ('new', 'done');\nALTER TYPE public.status ADD VALUE 'late';") == [
        {"op": "create_enum", "name": "status", "values": ["new", "done"]},
        {"op": "add_enum_value", "name": "status", "value": "late"}]


@pytest.mark.parametrize("sql", [
    "ALTER TYPE s ADD VALUE 'x' BEFORE 'y';",
    "ALTER TYPE s ADD VALUE 'x' AFTER 'y';",
    "ALTER TYPE s ADD VALUE IF NOT EXISTS 'x';",
    "ALTER TYPE s RENAME VALUE 'a' TO 'b';",
    "CREATE TYPE c AS (a int);",
])
def test_unsupported_type_variants_are_refused_with_the_line(sql):
    errs = errors("SET client_encoding = 'UTF8';\n" + sql + "\n")
    assert len(errs) == 1 and errs[0][0] == 2, errs


def test_a_column_may_use_an_enum_made_earlier_in_the_script():
    p = parse_script("CREATE TYPE status AS ENUM ('new','done');\n"
                     "CREATE TABLE t (id int, s status NOT NULL DEFAULT 'new'::status);")
    assert p.errors == []
    c = col(table(p, "t"), "s")
    assert c["type"] == "status" and c["default"] == "new"
    assert [o["op"] for o in p.ops] == ["create_enum", "create_table"]


def test_an_unknown_type_is_still_refused():
    assert errors("CREATE TABLE t (id int, s mystery);")


# ---------------------------------------------------------------- mapping keeps the new ops

def mapped(text):
    p = parse_script(text)
    assert p.errors == [], p.errors
    return apply_mapping(p, suggest_mapping(p))


def test_mapping_passes_enums_views_and_column_ops_through_in_a_safe_order():
    r = mapped("CREATE TYPE status AS ENUM ('a','b');\nCREATE TABLE t (id int, s status);\nCREATE VIEW v AS SELECT id FROM t;")
    assert r.issues == []
    assert [o["op"] for o in r.ops] == ["create_enum", "create_table", "create_view"]


def test_mapping_refuses_a_view_over_a_table_it_renames():
    p = parse_script("CREATE TABLE \"Order\" (id int);\nCREATE VIEW v AS SELECT id FROM \"Order\";")
    assert p.errors == [], p.errors
    r = apply_mapping(p, suggest_mapping(p))
    assert r.issues and "view v" in r.issues[0].message


# ---------------------------------------------------------------- end to end

@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


@pytest.mark.django_db
@pytest.mark.usefixtures("master")
def test_script_with_type_table_view_and_default_loads_end_to_end():
    editor = User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)
    approver = User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)
    text = ("CREATE TYPE status AS ENUM ('new', 'done');\n"
            "CREATE TABLE ticket (code text PRIMARY KEY, state status, note text);\n"
            "ALTER TABLE ticket ALTER COLUMN state SET DEFAULT 'new';\n"
            "ALTER TABLE ticket ALTER COLUMN note SET NOT NULL;\n"
            "CREATE VIEW open_ticket AS SELECT code FROM ticket WHERE state = 'new';\n")
    s = service.upload(editor, "s.sql", text.encode())
    service.validate(editor, s.pk, {"tables": {"ticket": {"target": "ticket", "columns": {"code": "code", "state": "state", "note": "note"}, "drop": [], "refs": []}}})
    s.refresh_from_db()
    assert s.report["errors"] == [], s.report["errors"]
    ch = service.submit(editor, s.pk)
    ch = approvals.approve(approver, ch.pk)
    assert ch.status == "applied", ch.error
    snap = introspect.snapshot("master")
    assert snap.enums == {"status": ["new", "done"]} and "open_ticket" in snap.views
    cols = snap["ticket"]["columns"]
    from schema.util import pg_default_to_allowed
    assert cols["state"]["type"] == "status" and cols["note"]["nullable"] is False
    assert pg_default_to_allowed(cols["state"]["default"], "status") == (True, "new")
