"""DB-free tests for the pure DDL renderer. Shape mirrors introspect.graph()."""

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import resolve

from accounts.models import Role, User
from schema import introspect, views
from schema.export import to_sql

from . import pgsetup

RF = RequestFactory()

# Singularity's own state lives in the `singularity` schema / Django's app tables;
# none of it may leak into an export of the *designed* schema.
INTERNAL = (
    "django_migrations", "auth_user", "accounts_user", "accounts_mcptoken",
    "settings_app_setting", "settings_app_rule", "approvals_change",
    "audit_auditlog", "chat_conversation", "chat_message",
    "duplicates_duplicategroup", "scripts_script", "search_schemaembedding",
    "singularity.", "master.",
)

GRAPH = {
    "tables": [
        {
            "name": "province",
            "comment": "Provinces",
            "rowEstimate": 77,
            "columns": [
                {"name": "province_id", "type": "integer", "nullable": False, "primaryKey": True},
                {"name": "name", "type": "text", "nullable": True, "primaryKey": False},
            ],
        },
        {
            "name": "district",
            "comment": None,
            "rowEstimate": 928,
            "columns": [
                {"name": "district_id", "type": "integer", "nullable": False, "primaryKey": True},
                {"name": "province_id", "type": "integer", "nullable": False, "primaryKey": False},
                {"name": "name", "type": "text", "nullable": False, "primaryKey": False},
            ],
        },
        {
            "name": "subdistrict",
            "comment": None,
            "rowEstimate": 7435,
            "columns": [
                {"name": "subdistrict_id", "type": "integer", "nullable": False, "primaryKey": True},
                {"name": "district_id", "type": "integer", "nullable": False, "primaryKey": False},
            ],
        },
    ],
    "relations": [
        {"from": {"table": "district", "column": "province_id"},
         "to": {"table": "province", "column": "province_id"}},
        {"from": {"table": "subdistrict", "column": "district_id"},
         "to": {"table": "district", "column": "district_id"}},
    ],
}


def test_create_table_columns_and_pk():
    sql = to_sql(GRAPH)
    assert 'CREATE TABLE "province"' in sql
    assert '"province_id" integer NOT NULL' in sql
    assert '"district_id" integer NOT NULL' in sql
    assert 'PRIMARY KEY ("subdistrict_id")' in sql
    assert sql.count("CREATE TABLE") == 3


def test_nullable_omits_not_null():
    sql = to_sql(GRAPH)
    province_block = sql.split('CREATE TABLE "district"')[0]
    assert '"name" text NOT NULL' not in province_block
    assert '"name" text,' in province_block or '"name" text\n' in province_block


def test_foreign_keys_emitted_from_relations():
    sql = to_sql(GRAPH)
    assert (
        'ALTER TABLE "district" ADD FOREIGN KEY ("province_id") '
        'REFERENCES "province" ("province_id");'
    ) in sql
    assert (
        'ALTER TABLE "subdistrict" ADD FOREIGN KEY ("district_id") '
        'REFERENCES "district" ("district_id");'
    ) in sql


def test_composite_primary_key_ordering():
    graph = {"tables": [{
        "name": "link", "comment": None, "rowEstimate": 0,
        "columns": [
            {"name": "a_id", "type": "integer", "nullable": False, "primaryKey": True},
            {"name": "b_id", "type": "integer", "nullable": False, "primaryKey": True},
        ],
    }], "relations": []}
    assert 'PRIMARY KEY ("a_id", "b_id")' in to_sql(graph)


def test_identifiers_are_quoted_including_embedded_quotes():
    graph = {"tables": [{
        'name': 'we"ird', "comment": None, "rowEstimate": 0,
        "columns": [
            {"name": 'co"l', "type": "text", "nullable": False, "primaryKey": True},
        ],
    }], "relations": [
        {"from": {"table": 'we"ird', "column": 'co"l'},
         "to": {"table": 'ta"rget', "column": 'c"2'}},
    ]}
    sql = to_sql(graph)
    assert 'CREATE TABLE "we""ird"' in sql
    assert '"co""l" text NOT NULL' in sql
    assert 'ALTER TABLE "we""ird" ADD FOREIGN KEY ("co""l") REFERENCES "ta""rget" ("c""2");' in sql


def test_empty_graph_returns_empty_string():
    assert to_sql({"tables": [], "relations": []}) == ""
    assert to_sql({}) == ""


# ---- Task 2: the endpoint ---------------------------------------------------

def export_as(user, monkeypatch=None):
    """Call the real view with a hand-built request; no client, no DB."""
    if monkeypatch:
        monkeypatch.setattr(introspect, "graph", lambda: GRAPH)
    req = RF.get("/api/schema/export?format=sql")
    req.user = user
    return views.export(req)


def test_export_route_is_wired():
    assert resolve("/api/schema/export").func is views.export


def test_anonymous_export_is_unauthorized(monkeypatch):
    r = export_as(AnonymousUser(), monkeypatch)
    assert r.status_code == 401


def test_export_returns_sql_attachment(monkeypatch):
    r = export_as(User(role=Role.VIEWER), monkeypatch)
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/sql")
    assert r["Content-Disposition"] == 'attachment; filename="singularity-schema.sql"'
    body = r.content.decode()
    assert 'CREATE TABLE "province"' in body
    assert 'ALTER TABLE "district"' in body


def test_export_leaks_no_singularity_internals(monkeypatch):
    body = export_as(User(role=Role.VIEWER), monkeypatch).content.decode().lower()
    assert [name for name in INTERNAL if name in body] == []


# ---- Task 3: the round-trip -------------------------------------------------

SEED = """
CREATE TABLE master.province (province_id integer NOT NULL PRIMARY KEY, name text);
CREATE TABLE master.district (
  district_id integer NOT NULL PRIMARY KEY,
  province_id integer NOT NULL REFERENCES master.province (province_id),
  name text NOT NULL);
CREATE TABLE master.subdistrict (
  subdistrict_id integer NOT NULL PRIMARY KEY,
  district_id integer NOT NULL REFERENCES master.district (district_id));
"""

SCRATCH = "export_roundtrip"


def test_exported_sql_loads_into_a_real_postgres(master):
    """Introspect real tables, export them, load the export into a scratch DB, re-read it."""
    with pgsetup.connect("singularity_master_rw", master) as rw:
        rw.execute(SEED)

    r = export_as(User(role=Role.VIEWER))
    assert r.status_code == 200
    sql = r.content.decode()
    assert sql.count("CREATE TABLE") == 3

    with pgsetup.connect(pgsetup.ADMIN, "postgres") as admin:
        admin.execute(f"CREATE DATABASE {SCRATCH}")
    try:
        read_back = load_and_read(sql)
    finally:
        pgsetup.drop_db(SCRATCH)

    assert read_back["tables"] == {"province", "district", "subdistrict"}
    assert read_back["pks"] == {("province", "province_id"), ("district", "district_id"),
                                ("subdistrict", "subdistrict_id")}
    assert read_back["fks"] == {("district", "province"), ("subdistrict", "district")}
    assert read_back["columns"] >= {
        ("district", "province_id", "integer", "NO"),
        ("province", "name", "text", "YES"),
        ("subdistrict", "district_id", "integer", "NO"),
    }


def load_and_read(sql):
    """Execute the export in the scratch DB and read the result back from the catalogs."""
    with pgsetup.connect(pgsetup.ADMIN, SCRATCH) as db:
        db.execute(sql)  # any bad statement raises here: psycopg3 stops on the first error
        tables = {t for (t,) in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        columns = set(db.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = 'public'"))
        pks = set(db.execute(
            "SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu USING (constraint_name) "
            "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'"))
        fks = set(db.execute(
            "SELECT child.relname, parent.relname FROM pg_constraint c "
            "JOIN pg_class child ON child.oid = c.conrelid "
            "JOIN pg_class parent ON parent.oid = c.confrelid WHERE c.contype = 'f'"))
    return {"tables": tables, "columns": columns, "pks": pks, "fks": fks}
