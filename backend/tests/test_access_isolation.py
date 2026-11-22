"""SQL isolation (per-database access, Task 6): `query`, intelligent query, raw SQL and scripts cannot read or name
databases the user lacks.

Two databases, `master` and `crm`, with data; the user can read `master` only. A database the user cannot reach is
refused with the very text of one that does not exist.
"""
import pytest

from accounts.models import Role, SchemaGrant, User
from approvals import rawsql, service
from approvals.models import Change
from mcp_server import tools
from schema import conn as dbconn
from schema import isolation, query
from scripts import storage
from scripts import validate as validate_mod

from . import pgsetup

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master"), pytest.mark.no_default_access]

REASON = "fix the names in this database please"


def build(*statements):
    with dbconn.rw_connection(autocommit=True) as c:
        for st in statements:
            c.execute(st)


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect("singularity_master_rw", master_db) as rw:
        for (s,) in rw.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            rw.execute(f'DROP SCHEMA "{s}" CASCADE')


@pytest.fixture(autouse=True)
def fresh_settings():
    from config.siconfig import invalidate_overrides
    invalidate_overrides()
    yield
    invalidate_overrides()


@pytest.fixture
def dbs():
    from .test_executor_schemas import make_crm
    make_crm()
    build(
        "CREATE TABLE master.t (code text, name text)",
        "INSERT INTO master.t VALUES ('M1', 'Alpha'), ('M2', 'Beta')",
        "CREATE TABLE crm.t (code text, name text)",
        "INSERT INTO crm.t VALUES ('C1', 'Secret Co')",
        "CREATE TABLE crm.only_crm (x text)",
        "INSERT INTO crm.only_crm VALUES ('hidden')",
    )


def make_user(role, email=None, **grants):
    email = email or f"{role}-{User.objects.count()}@x.com"
    u = User.objects.create(username=email, email=email, role=role)
    for schema, level in grants.items():
        SchemaGrant.objects.create(user=u, schema=schema, role=level)
    return u


@pytest.fixture
def viewer(dbs):
    return make_user(Role.VIEWER, master=Role.VIEWER)


@pytest.fixture
def editor(dbs):
    return make_user(Role.EDITOR, master=Role.EDITOR)


@pytest.fixture
def admin(dbs):
    return make_user(Role.ADMIN, "admin@x.com")


def run(user, sql, **kw):
    return query.run_select(sql, user=user, **kw)


def refused(user, sql, **kw) -> str:
    with pytest.raises(query.QueryError) as e:
        run(user, sql, **kw)
    return str(e.value)


def same_text(hidden: str, missing: str, h="crm", m="nope") -> bool:
    return hidden.replace(h, "X") == missing.replace(m, "X")


# ---- query --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM {db}.t",
    "SELECT {db}.t.code FROM t",
    "SELECT * FROM t JOIN {db}.t c USING (code)",
    "SELECT * FROM t WHERE code IN (SELECT code FROM {db}.t)",
    "WITH x AS (SELECT * FROM {db}.t) SELECT * FROM x",
    "SELECT (SELECT count(*) FROM {db}.t)",
    "SELECT * FROM {db}.f()",
    "SELECT NULL::{db}.mytype",
    "SELECT '{db}.t'::regclass",
    "SELECT to_regclass('{db}.t')",
    "SELECT to_regnamespace('{db}')",
    "SELECT has_schema_privilege('{db}', 'usage')",
])
def test_query_qualified_name_in_other_database_refused(viewer, sql):
    hidden = refused(viewer, sql.format(db="crm"))
    missing = refused(viewer, sql.format(db="nope"))
    assert hidden == "Unknown schema 'crm'." and same_text(hidden, missing)
    assert "Secret" not in hidden and "only_crm" not in hidden


def test_query_view_expansion_refused(viewer, admin):
    build("CREATE VIEW master.v AS SELECT * FROM crm.t")
    msg = refused(viewer, "SELECT * FROM v")
    assert msg == isolation.PLAN_REFUSAL and "crm" not in msg
    assert run(admin, "SELECT name FROM v")["rows"] == [["Secret Co"]]


@pytest.mark.parametrize("sql", [
    "SELECT nspname FROM pg_namespace",
    "SELECT nspname FROM pg_catalog.pg_namespace",
    "SELECT relname FROM pg_class",
    "SELECT tablename FROM pg_tables",
    "SELECT viewname FROM pg_views",
    "SELECT rolname FROM pg_roles",
    "SELECT schema_name FROM information_schema.schemata",
    "SELECT table_name FROM information_schema.tables",
    "SELECT column_name FROM information_schema.columns",
])
def test_query_catalog_listing_refused_for_non_admin(viewer, admin, sql):
    msg = refused(viewer, sql)
    assert msg == isolation.CATALOG_REFUSAL and "crm" not in msg
    assert run(admin, sql)["rows"]          # admins may read the catalogs


def test_query_catalog_functions_that_name_objects_refused(viewer):
    for sql in ("SELECT pg_get_viewdef(c) FROM generate_series(1, 3) c", "SELECT 16384::regclass",
                "SELECT obj_description(1)", "SELECT query_to_xml('select * from crm.t', true, false, '')",
                "SELECT ts_stat('select 1')", "SELECT format_type(23, NULL)"):
        msg = refused(viewer, sql)
        assert "Secret" not in msg and "only_crm" not in msg


def test_query_own_database_still_works(viewer):
    assert run(viewer, "SELECT code FROM t ORDER BY 1")["rows"] == [["M1"], ["M2"]]
    assert run(viewer, "SELECT code FROM master.t WHERE code = lower('M1') OR true ORDER BY 1")["rows"][0] == ["M1"]
    assert run(viewer, "SELECT now() IS NOT NULL, pg_catalog.upper('a'), 'x'::text, 1::pg_catalog.int4")["rows"]
    assert run(viewer, "SELECT g FROM generate_series(1, 2) g")["rows"] == [[1], [2]]
    assert run(viewer, "SELECT 't'::regclass IS NOT NULL")["rows"] == [[True]]


def test_query_needs_the_database_itself(viewer):
    hidden = refused_unknown(viewer, "crm")
    missing = refused_unknown(viewer, "nope")
    assert same_text(hidden, missing)


def refused_unknown(user, schema):
    from schema import introspect
    with pytest.raises(introspect.UnknownSchema) as e:
        run(user, "SELECT 1", schema=schema)
    return str(e.value)


def test_search_path_tricks_refused(viewer):
    # unchanged: query's single-SELECT rule already refuses SET and a second statement
    assert refused(viewer, "SET search_path = crm") == "only SELECT queries are allowed"
    assert refused(viewer, "SELECT 1; SET search_path = crm; SELECT * FROM t") == "only a single statement is allowed"
    # a setting changed inside the statement, or a schema-qualified function, is refused
    assert "set_config" in refused(viewer, "SELECT set_config('search_path', 'crm', true), to_regclass('t')")
    assert refused(viewer, "SELECT crm.anything()") == "Unknown schema 'crm'."


def test_query_admin_reaches_everything(admin):
    assert run(admin, "SELECT name FROM crm.t")["rows"] == [["Secret Co"]]


def test_mcp_query_uses_the_same_check(viewer):
    with pytest.raises(tools.ToolFailure) as e:
        tools.run_query(viewer, "SELECT * FROM crm.only_crm")
    with pytest.raises(tools.ToolFailure) as e2:
        tools.run_query(viewer, "SELECT * FROM nope.only_crm")
    assert same_text(str(e.value), str(e2.value))
    assert tools.run_query(viewer, "SELECT count(*) FROM t")["rows"] == [[2]]


def test_web_query_uses_the_same_check(viewer, client):
    client.force_login(viewer)
    r = client.post("/api/query", {"sql": "SELECT * FROM crm.t"}, content_type="application/json")
    r2 = client.post("/api/query", {"sql": "SELECT * FROM nope.t"}, content_type="application/json")
    assert r.status_code == r2.status_code == 400 and same_text(r.json()["error"], r2.json()["error"])
    ok = client.post("/api/query", {"sql": "SELECT count(*) FROM t"}, content_type="application/json")
    assert ok.status_code == 200 and ok.json()["rows"] == [[2]]


# ---- the plan check on its own (what the text check could miss) -----------------------------------------------

@pytest.mark.parametrize("body", [
    "SELECT * FROM crm.t",
    "WITH x AS MATERIALIZED (SELECT * FROM crm.t) SELECT * FROM x",
    "SELECT * FROM master.t WHERE code IN (SELECT code FROM crm.t)",
    "SELECT * FROM master.t m WHERE EXISTS (SELECT 1 FROM crm.t c WHERE c.code = m.code)",
    "SELECT (SELECT count(*) FROM crm.t)",
    "SELECT * FROM master.t m JOIN crm.t c ON c.code <> m.code",
    "SELECT * FROM master.t UNION ALL SELECT * FROM crm.t",
    "SELECT * FROM (SELECT * FROM crm.only_crm) s",
    "SELECT nspname FROM pg_namespace",
    "SELECT table_name FROM information_schema.tables",
])
def test_explain_walks_the_whole_plan(dbs, body):
    with dbconn.ro_connection() as c:
        c.execute("SET TRANSACTION READ ONLY")
        c.execute("SET LOCAL search_path = master")
        with pytest.raises(query.QueryError) as e:
            isolation.check_plan(c, body, {"master"}, is_admin=False)
        isolation.check_plan(c, body, {"master"}, is_admin=True)   # admins: nothing checked
    assert "crm" not in str(e.value)


def test_explain_allows_own_database(dbs):
    with dbconn.ro_connection() as c:
        c.execute("SET LOCAL search_path = master")
        isolation.check_plan(c, "SELECT * FROM t JOIN generate_series(1, 2) g ON true", {"master"}, is_admin=False)


# ---- raw SQL ----------------------------------------------------------------------------------------------------

@pytest.fixture
def no_dry_run(monkeypatch):
    calls = []
    real = rawsql.dry_run

    def spy(statements, schema):
        calls.append(schema)
        return real(statements, schema)

    monkeypatch.setattr(rawsql, "dry_run", spy)
    return calls


def sql_refusal(user, sql, code="invalid", **kw):
    with pytest.raises(service.ChangeError) as e:
        service.propose_sql(user, sql, REASON, **kw)
    assert e.value.code == code
    return e.value.message


@pytest.mark.parametrize("sql", [
    "UPDATE {db}.t SET name = 'x'",
    "INSERT INTO master.t SELECT * FROM {db}.t",
    "UPDATE master.t SET name = (SELECT name FROM {db}.t LIMIT 1)",
    "COMMENT ON TABLE {db}.t IS 'x'",
    "ALTER TABLE master.t SET SCHEMA {db}",
    "GRANT SELECT ON ALL TABLES IN SCHEMA {db} TO PUBLIC",
    "CREATE VIEW master.v2 AS SELECT * FROM {db}.t",
    "SELECT setval('{db}.s', 1)",
    "CREATE FUNCTION master.f() RETURNS bigint LANGUAGE sql AS 'select count(*) from {db}.t'",
    "DO $$ BEGIN UPDATE {db}.t SET name = 'x'; END $$",
    "CREATE TABLE master.t2 (a {db}.mytype)",
])
def test_propose_sql_naming_other_database_refused_before_dry_run(editor, no_dry_run, sql):
    hidden = sql_refusal(editor, sql.format(db="crm"))
    missing = sql_refusal(editor, sql.format(db="nope"))
    assert "Unknown schema 'crm'." in hidden and same_text(hidden, missing)
    assert no_dry_run == [] and Change.objects.count() == 0


def test_propose_sql_readable_but_not_editable_gets_role_refusal(dbs, no_dry_run):
    u = make_user(Role.EDITOR, master=Role.EDITOR, crm=Role.VIEWER)
    msg = sql_refusal(u, "UPDATE crm.t SET name = 'x'", code="forbidden")
    assert "editor" in msg and no_dry_run == []


def test_propose_sql_target_database_hidden_equals_missing(viewer, no_dry_run):
    u = make_user(Role.EDITOR, master=Role.EDITOR)
    hidden = sql_refusal(u, "UPDATE t SET name = 'x'", schema="crm")
    missing = sql_refusal(u, "UPDATE t SET name = 'x'", schema="nope")
    assert same_text(hidden, missing) and no_dry_run == []


@pytest.mark.parametrize("sql", [
    "INSERT INTO master.t SELECT nspname, '' FROM pg_namespace",
    "INSERT INTO master.t SELECT table_name, '' FROM information_schema.tables",
    "DO $$ BEGIN EXECUTE 'UPDATE crm.t SET name = 1'; END $$",
    "INSERT INTO master.t VALUES (to_regclass('crm.t')::text, '')",
    "INSERT INTO master.t SELECT pg_get_viewdef(16384), ''",
])
def test_propose_sql_catalog_and_unclear_shapes_refused(editor, no_dry_run, sql):
    msg = sql_refusal(editor, sql)
    assert "Secret" not in msg and no_dry_run == []


def test_propose_sql_own_database_still_works(editor, no_dry_run):
    ch = service.propose_sql(editor, "UPDATE master.t SET name = 'x' WHERE code = 'M1'; UPDATE t SET name = upper(name)",
                             REASON)
    assert ch.status == Change.Status.PENDING and no_dry_run == ["master"]


def test_propose_sql_admin_names_anything(admin, no_dry_run):
    ch = service.propose_sql(admin, "UPDATE crm.t SET name = 'y'", REASON)
    assert ch.status == Change.Status.PENDING and no_dry_run == ["master"]


# ---- scripts ----------------------------------------------------------------------------------------------------

@pytest.fixture
def no_script_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(validate_mod.executor, "execute", lambda *a, **k: calls.append(k) or None)
    return calls


def _validate(user, text, schema="master"):
    script = storage.save(user, "dump.sql", text.encode("utf-8"))
    from scripts import service as scripts_service
    return scripts_service.validate(user, script.pk, None, schema)


@pytest.mark.parametrize("view", [
    "CREATE VIEW v AS SELECT '{db}.t'::regclass AS x;\n",
    "CREATE VIEW v AS SELECT to_regclass('{db}.only_crm') AS x;\n",
])
def test_script_naming_other_database_refused(editor, no_script_dry_run, view):
    hidden = _validate(editor, view.format(db="crm"))
    missing = _validate(editor, view.format(db="nope"))
    assert not hidden["ok"] and not missing["ok"]
    h = [e["message"] for e in hidden["errors"] if e["kind"] == "access"]
    m = [e["message"] for e in missing["errors"] if e["kind"] == "access"]
    assert h and "Unknown schema 'crm'." in h[0] and same_text(h[0], m[0])
    assert no_script_dry_run == []


def test_script_reading_the_catalogs_refused(editor, no_script_dry_run):
    r = _validate(editor, "CREATE VIEW v AS SELECT nspname FROM pg_catalog.pg_namespace;\n")
    assert not r["ok"] and any(isolation.CATALOG_REFUSAL in e["message"] for e in r["errors"])
    assert no_script_dry_run == []


def test_propose_change_view_over_other_database_refused(editor):
    def go(db):
        with pytest.raises(service.ChangeError) as e:
            service.propose(editor, [{"op": "create_view", "name": "v", "query": f"SELECT * FROM {db}.t"}], source="mcp")
        return e.value.message
    assert same_text(go("crm"), go("nope")) and "Unknown schema 'crm'." in go("crm")
    assert Change.objects.count() == 0


# ---- intelligent query ----------------------------------------------------------------------------------------

def test_iquery_generated_sql_checked(viewer, monkeypatch, embed):
    from django.core.cache import cache

    from chat import providers
    from iquery import memory, suggest

    cache.clear()
    replies = iter(["SELECT * FROM crm.only_crm", "SELECT * FROM crm.t"])
    monkeypatch.setattr(providers, "complete", lambda *a, **k: {"content": next(replies), "tool_calls": []})
    monkeypatch.setattr(suggest, "_model_name", lambda cfg: "m")
    r = suggest.suggest(viewer, "master", ["show me everything"], "")
    assert r["sql"] is None and r["reason"] == "Unknown schema 'crm'."
    assert memory.explains("master", "SELECT * FROM crm.t", user=viewer) == "Unknown schema 'crm'."
    build("CREATE VIEW master.v AS SELECT * FROM crm.t")
    assert memory.explains("master", "SELECT * FROM v", user=viewer) == isolation.PLAN_REFUSAL
    assert memory.explains("master", "SELECT * FROM t", user=viewer) is None
    with pytest.raises(ValueError):
        memory.learn(viewer, "master", ["x"], "SELECT * FROM crm.t", [])


def test_iquery_saved_examples_that_reach_other_databases_are_not_shown(viewer, embed):
    from iquery import memory
    from iquery.models import QueryExample

    from .conftest import fake_vector

    for sql in ("SELECT * FROM crm.t", "SELECT * FROM t"):
        QueryExample.objects.create(schema="master", comments="all rows", sql=sql, sql_norm=memory.normalize_sql(sql),
                                    values=[], embedding=fake_vector("all rows"), status="active")
    got = memory.examples("master", ["all rows"], 5, user=viewer)
    assert [e.sql for e in got] == ["SELECT * FROM t"]
    assert QueryExample.objects.count() == 2            # hidden from this user, not deleted


# ---- the accepted gap -----------------------------------------------------------------------------------------

@pytest.mark.known_gap
def test_known_gap_function_reading_other_database_is_not_detected(viewer):
    """ACCEPTED GAP (spec "Raw SQL and query cannot reach other databases"; README "Database access"): a function
    in a readable database that itself reads another database is not detected: the plan shows only the call.
    This test documents the gap; if it starts failing, the gap was closed and the README should say so."""
    build("CREATE FUNCTION master.peek() RETURNS text LANGUAGE plpgsql STABLE AS "
          "$$ BEGIN RETURN (SELECT name FROM crm.t LIMIT 1); END $$")
    assert run(viewer, "SELECT peek()")["rows"] == [["Secret Co"]]


# ---- the text helpers -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql,names", [
    ("SELECT * FROM crm.t JOIN master.u ON true", {"crm", "master"}),
    ("SELECT crm.t.c FROM t", {"crm"}),
    ("SELECT NULL::crm.ty, crm.f(), 1 OPERATOR(crm.+) 2", {"crm"}),
    ("COMMENT ON COLUMN crm.t.c IS 'x'", {"crm"}),
    ("COMMENT ON SCHEMA crm IS 'x'", {"crm"}),
    ("ALTER TABLE master.t SET SCHEMA crm", {"master", "crm"}),
    ("GRANT USAGE ON SCHEMA crm TO PUBLIC", {"crm"}),
    ("DROP TYPE crm.e", {"crm"}),
    ("CREATE TRIGGER g BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION crm.tf()", {"crm"}),
    ("SELECT nextval('crm.s'), 'crm.t'::regclass", {"crm"}),
    ("DO $$ BEGIN PERFORM 1 FROM crm.t; END $$", {"crm"}),
])
def test_named_schemas_covers_qualified_names(sql, names):
    assert isolation.named_schemas(sql) - isolation.SYSTEM == names


@pytest.mark.parametrize("sql", [
    "DO $$ BEGIN EXECUTE 'select 1'; END $$", "SELECT query_to_xml('x', true, false, '')", "SELECT (",
    "CREATE EXTENSION x", "SELECT 16384::regclass", "SELECT pg_get_viewdef(1)",
])
def test_named_schemas_fails_closed(sql):
    with pytest.raises(isolation.Unclear):
        isolation.named_schemas(sql)


def test_catalog_names_leak():
    assert isolation.catalog_names_leak("SELECT * FROM pg_namespace")
    assert isolation.catalog_names_leak("SELECT * FROM information_schema.columns")
    assert isolation.catalog_names_leak("SELECT (")
    assert not isolation.catalog_names_leak("SELECT pg_catalog.lower(name), 1::pg_catalog.int4 FROM t")


# ---- fix round 1: settings in bodies, reg* output functions, views that launder data ----------------------------

SETTINGS_TRICKS = [
    "DO $$ BEGIN SET/**/search_path = crm; RAISE EXCEPTION 'leak: %', (SELECT string_agg(name, ',') FROM t); END $$",
    'DO $$ BEGIN SET U&"\\0073earch_path" = crm; INSERT INTO master.t SELECT * FROM t; END $$',
    'DO $$ BEGIN SET "search_path" = crm; INSERT INTO master.t SELECT * FROM t; END $$',
    "DO $$ BEGIN SET LOCAL search_path TO crm; INSERT INTO master.t SELECT * FROM t; END $$",
    "DO $$ BEGIN PERFORM set_config('search_path', 'crm', true); INSERT INTO master.t SELECT * FROM t; END $$",
    "DO $$ BEGIN RESET ALL; END $$",
    "CREATE FUNCTION master.f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN SET/**/search_path = crm; "
    "INSERT INTO master.t SELECT * FROM t; END $$",
    "CREATE FUNCTION master.f() RETURNS void LANGUAGE sql AS 'SET search_path = crm; INSERT INTO master.t SELECT * FROM t'",
    "CREATE FUNCTION master.f() RETURNS bigint LANGUAGE sql SET search_path = crm AS 'select count(*) from t'",
    "ALTER FUNCTION master.f() SET search_path = crm",
    "SET/**/search_path = crm",
    "SELECT set_config('search_path', 'crm', true)",
]


@pytest.mark.parametrize("sql", SETTINGS_TRICKS)
def test_search_path_changes_in_bodies_refused_before_dry_run(editor, no_dry_run, sql):
    msg = sql_refusal(editor, sql)
    assert "Secret" not in msg and no_dry_run == [] and Change.objects.count() == 0


@pytest.mark.parametrize("sql", [s for s in SETTINGS_TRICKS if "set_config" not in s and "RESET" not in s])
def test_search_path_changes_in_bodies_refused_for_admins_too(admin, no_dry_run, sql):
    # the fence on settings is for everyone (rawsql.parse_statements reads them from the parsed bodies)
    msg = sql_refusal(admin, sql)
    assert "search_path" in msg and no_dry_run == []


REG_OUTPUT = [
    "SELECT regclassout(o::oid)::text FROM generate_series(1, 30000) o",
    "SELECT regnamespaceout(o::oid)::text FROM generate_series(1, 30000) o",
    "SELECT regtypeout(o::oid)::text FROM generate_series(1, 30000) o",
    "SELECT regprocout(o::oid)::text FROM generate_series(1, 30000) o",
    "SELECT regclassin('crm.t')",
    "SELECT oidvectortypes('23 25'::oidvector)",
    "SELECT format_type(o::oid, NULL) FROM generate_series(1, 30000) o",
]


@pytest.mark.parametrize("sql", REG_OUTPUT)
def test_reg_output_functions_refused_for_non_admin(viewer, admin, sql):
    msg = refused(viewer, sql)
    assert "crm" not in msg.replace("'crm.t'", "") and "only_crm" not in msg
    run(admin, sql)                                     # admins are not checked


@pytest.mark.parametrize("sql", REG_OUTPUT)
def test_reg_output_functions_refused_in_raw_sql(editor, no_dry_run, sql):
    msg = sql_refusal(editor, f"DO $$ BEGIN RAISE NOTICE '%', ({sql} LIMIT 1); END $$")
    assert "only_crm" not in msg and no_dry_run == []


def test_common_functions_still_work(viewer):
    r = run(viewer, "SELECT max(code), min(code), count(*), string_agg(name, ',' ORDER BY name), json_agg(code), "
                    "coalesce(NULL, 1), to_char(date '2026-01-02', 'YYYY'), length(name), upper(lower(name)), "
                    "row_number() OVER () FROM t GROUP BY name ORDER BY 1")
    assert len(r["rows"]) == 2
    assert run(viewer, "SELECT to_tsvector('english', name) IS NOT NULL, jsonb_build_object('a', 1), "
                       "date_trunc('day', now()) IS NOT NULL, round(1.25, 1), abs(-1), md5('x') FROM t")["rows"]


def test_risky_function_list_covers_the_server(dbs):
    """Every pg_catalog function (outside pg_*) taking or returning oid/oidvector/reg* is refused, unless it is a
    harmless oid comparison/aggregate. Fails after a Postgres upgrade adds one: add it to isolation._BY_TYPE."""
    with dbconn.ro_connection() as c:
        names = {r["proname"] for r in c.execute(
            "WITH t AS (SELECT unnest(ARRAY['oid','oidvector','regclass','regnamespace','regproc','regprocedure',"
            "'regoper','regoperator','regtype','regrole','regcollation']::regtype[])::oid AS t) "
            "SELECT DISTINCT proname FROM pg_proc p WHERE pronamespace IN ('pg_catalog'::regnamespace, "
            "'information_schema'::regnamespace) AND proname !~ '^_?pg_' AND (EXISTS (SELECT 1 FROM "
            "unnest(p.proargtypes::oid[]) a WHERE a IN (SELECT t FROM t)) OR p.prorettype IN (SELECT t FROM t))")}
    missing = {n for n in names - isolation._OID_SAFE
               if n not in isolation._BY_TYPE and not isolation._BY_TYPE_PATTERN.match(n)}
    assert missing == set()


@pytest.fixture
def laundering_view(dbs):
    build("CREATE VIEW master.v_crm AS SELECT * FROM crm.t", "CREATE VIEW master.v_m AS SELECT * FROM master.t")


@pytest.mark.parametrize("sql", [
    "DO $$ BEGIN RAISE EXCEPTION '%', (SELECT string_agg(name, ',') FROM master.v_crm); END $$",
    "SELECT (SELECT name FROM v_crm LIMIT 1)::int",
    "INSERT INTO master.t SELECT * FROM v_crm",
    "CREATE VIEW master.v2 AS SELECT * FROM v_crm",
])
def test_raw_sql_view_over_hidden_table_refused(editor, laundering_view, no_dry_run, sql):
    msg = sql_refusal(editor, sql)
    assert msg.endswith(isolation.VIEW_REFUSAL) and "Secret" not in msg and "crm." not in msg.replace("v_crm", "")
    assert no_dry_run == []


def test_raw_sql_view_inside_own_database_still_works(editor, laundering_view, no_dry_run):
    ch = service.propose_sql(editor, "INSERT INTO master.t SELECT * FROM v_m", REASON)
    assert ch.status == Change.Status.PENDING and no_dry_run == ["master"]


def test_raw_sql_view_over_hidden_table_admin_works(admin, laundering_view, no_dry_run):
    assert service.propose_sql(admin, "INSERT INTO master.t SELECT * FROM v_crm", REASON).status == Change.Status.PENDING


def test_raw_sql_inheritance_child_in_hidden_database_refused(editor, no_dry_run):
    build("CREATE TABLE master.parent (code text)", "CREATE TABLE crm.child () INHERITS (master.parent)",
          "INSERT INTO crm.child VALUES ('secret')")
    msg = sql_refusal(editor, "DO $$ BEGIN RAISE EXCEPTION '%', (SELECT string_agg(code, ',') FROM parent); END $$")
    assert msg.endswith(isolation.VIEW_REFUSAL) and "secret" not in msg and no_dry_run == []


def test_view_op_over_laundering_view_refused(editor, laundering_view):
    with pytest.raises(service.ChangeError) as e:
        service.propose(editor, [{"op": "create_view", "name": "v3", "query": "SELECT * FROM v_crm"}], source="mcp")
    assert e.value.message.endswith(isolation.VIEW_REFUSAL)


def test_query_path_unchanged_for_laundering_view(viewer, laundering_view):
    assert refused(viewer, "SELECT * FROM v_crm") == isolation.PLAN_REFUSAL
    assert run(viewer, "SELECT count(*) FROM v_m")["rows"] == [[2]]
