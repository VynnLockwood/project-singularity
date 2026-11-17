"""Per-database access on reads, exports and backups: the schema web app and the MCP read tools.

A database the user cannot reach answers exactly like one that does not exist; a readable database where
the user lacks the level answers with a role-style refusal (403 on the web, the role text over MCP).
"""
import json
import pathlib

import pytest
from django.test import RequestFactory
from django.urls import resolve

from accounts import access
from accounts.models import Role, SchemaGrant, User
from approvals import service
from mcp_server import tools
from schema import backup, introspect

from . import pgsetup

RF = RequestFactory()

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master"), pytest.mark.no_default_access]

HIDDEN = "crm"      # exists, the user has no grant there
MISSING = "nope"    # does not exist at all
TABLE = {"op": "create_table", "name": "t", "columns": [{"name": "code", "type": "text"}, {"name": "name", "type": "text"}]}
ONLY_CRM = {"op": "create_table", "name": "only_crm", "columns": [{"name": "x", "type": "text"}]}


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect("singularity_master_rw", master_db) as rw:
        for (s,) in rw.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            rw.execute(f'DROP SCHEMA "{s}" CASCADE')


def make_user(role, name=None, **kw):
    name = name or f"{role}-{User.objects.count()}@x.com"
    return User.objects.create(username=name, email=name, role=role, **kw)


def grant(u, schema, role):
    SchemaGrant.objects.update_or_create(user=u, schema=schema, defaults={"role": role})
    return u


def apply(user, ops, schema="master"):
    ch = service.propose(user, ops, source="web", schema=schema)
    assert ch.status == "pending", ch.check_result
    return service.approve(user, ch.pk)


@pytest.fixture
def admin():
    return make_user(Role.ADMIN, "admin@x.com")


@pytest.fixture
def dbs(admin):
    """`t` in master and in crm (with rows), plus `only_crm` that exists in crm alone."""
    apply(admin, [{"op": "create_schema", "name": "crm", "comment": "customers"}])
    for schema in ("master", "crm"):
        apply(admin, [TABLE], schema)
    apply(admin, [ONLY_CRM], "crm")
    apply(admin, [{"op": "insert_rows", "table": "t", "rows": [{"code": "M1", "name": "Alpha"}]}])
    apply(admin, [{"op": "insert_rows", "table": "t", "rows": [{"code": "C1", "name": "Acme"}]}], "crm")
    return admin


def no_grant(u):
    return grant(u, "master", Role.APPROVER)


def star_with_deny(u):
    grant(u, access.ALL, Role.APPROVER)
    return grant(u, HIDDEN, access.NONE)


SETUPS = {"no_grant": no_grant, "star_with_deny": star_with_deny}


@pytest.fixture(params=list(SETUPS))
def outsider(request, dbs):
    """A global approver (the most a non-admin can be) who cannot reach crm."""
    u = make_user(Role.APPROVER)
    SETUPS[request.param](u)
    return u


class web:
    """Calls the views as `user` through the URL map, like the export tests do (RequestFactory): a test Client
    closes the test's database connection when a streamed response ends."""

    def __init__(self, user):
        self.user = user

    def _call(self, req):
        req.user = self.user
        match = resolve(req.path)
        resp = match.func(req, *match.args, **match.kwargs)
        if not resp.streaming:
            resp.json = lambda: json.loads(resp.content)
        return resp

    def get(self, path, params=None):
        return self._call(RF.get(path, params or {}))

    def post(self, path, data, content_type):
        return self._call(RF.post(path, data, content_type=content_type))


# ---- web --------------------------------------------------------------------------------------

def _get(path, **params):
    return lambda c, schema: c.get(path, {**params, "schema": schema})


WEB = {
    "tables": _get("/api/schema/tables"),
    "graph": _get("/api/schema/graph"),
    "export_structure": _get("/api/schema/export", data="0"),
    "export_data": _get("/api/schema/export", data="1"),
    "export_copy": _get("/api/schema/export", data="1", format="copy"),
    "table_detail": _get("/api/schema/tables/t"),
    "table_detail_only_in_hidden": _get("/api/schema/tables/only_crm"),
    "table_rows": _get("/api/schema/tables/t/rows"),
    "table_rows_only_in_hidden": _get("/api/schema/tables/only_crm/rows"),
    "table_options": _get("/api/schema/tables/t/options"),
    "table_export_csv": _get("/api/schema/tables/t/export.csv"),
    "table_export_csv_only_in_hidden": _get("/api/schema/tables/only_crm/export.csv"),
    "run_query": lambda c, schema: c.post("/api/query", json.dumps({"sql": "SELECT 1 AS one", "schema": schema}),
                                          content_type="application/json"),
    "run_query_table": lambda c, schema: c.post("/api/query", json.dumps({"sql": "SELECT * FROM only_crm", "schema": schema}),
                                                content_type="application/json"),
}


def body(resp) -> bytes:
    """The whole response; a streamed export is read to the end (that also ends its transaction)."""
    return b"".join(resp.streaming_content) if resp.streaming else resp.content


def status(resp) -> int:
    body(resp)
    return resp.status_code


def _web_answer(resp, name):
    return resp.status_code, body(resp).decode().replace(name, "<db>")


@pytest.mark.parametrize("endpoint", list(WEB))
def test_hidden_equals_missing_web(outsider, endpoint):
    c = web(outsider)
    hidden = _web_answer(WEB[endpoint](c, HIDDEN), HIDDEN)
    missing = _web_answer(WEB[endpoint](c, MISSING), MISSING)
    assert hidden == missing
    assert hidden[0] == 400 and json.loads(hidden[1]) == {"error": "Unknown schema '<db>'."}


def test_every_schema_view_is_covered():
    """Each view in schema/views.py that takes a schema has a hidden-equals-missing case above."""
    from schema import views

    covered = {k.split("_only_in_hidden")[0] for k in WEB} | {"export"}
    takes_schema = {n for n, f in vars(views).items() if callable(f) and getattr(f, "__module__", "") == views.__name__
                    and not n.startswith("_") and n != "schemas"}
    assert takes_schema <= covered, takes_schema - covered


@pytest.mark.parametrize("endpoint", [k for k in WEB if "only_in_hidden" not in k and k != "run_query_table"])
def test_viewer_grant_reads_and_admin_reaches_everything(dbs, endpoint):
    v = grant(make_user(Role.EDITOR), HIDDEN, Role.EDITOR)
    assert status(WEB[endpoint](web(v), HIDDEN)) == 200
    assert status(WEB[endpoint](web(dbs), HIDDEN)) == 200   # admin, no grants


def test_inactive_user_reaches_nothing(dbs):
    u = grant(make_user(Role.VIEWER), access.ALL, Role.VIEWER)
    u.is_active = False
    u.save()
    with pytest.raises(tools.ToolFailure, match="Unknown schema 'crm'"):
        tools.list_tables(u, HIDDEN)
    assert tools.list_schemas(u) == {"schemas": []}


def test_list_schemas_only_readable(dbs):
    u = grant(make_user(Role.VIEWER), "master", Role.VIEWER)
    r = web(u).get("/api/schema/schemas")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["schemas"]] == ["master"]
    assert r.json()["schemas"][0]["tables"] == 1
    assert tools.list_schemas(u) == r.json()

    deny = star_with_deny(make_user(Role.APPROVER))
    assert [s["name"] for s in tools.list_schemas(deny)["schemas"]] == ["master"]

    only_crm = grant(make_user(Role.VIEWER), HIDDEN, Role.VIEWER)
    got = web(only_crm).get("/api/schema/schemas").json()["schemas"]
    assert got == [{"name": "crm", "tables": 2, "comment": "customers"}]

    assert [s["name"] for s in web(dbs).get("/api/schema/schemas").json()["schemas"]] == ["master", "crm"]
    assert web(make_user(Role.EDITOR)).get("/api/schema/schemas").json() == {"schemas": []}


def test_viewer_grant_cannot_export_with_data(dbs):
    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    c = web(u)
    refusal = {"error": "This needs the editor role on 'crm'; you have viewer there."}
    r = c.get("/api/schema/export", {"schema": HIDDEN, "data": "1"})
    assert r.status_code == 403 and r.json() == refusal
    r = c.get("/api/schema/tables/t/export.csv", {"schema": HIDDEN})
    assert r.status_code == 403 and r.json() == refusal
    assert status(c.get("/api/schema/export", {"schema": HIDDEN, "data": "0"})) == 200
    assert status(c.get("/api/schema/export", {"schema": "master", "data": "1"})) == 200

    with pytest.raises(tools.ToolFailure) as e:
        tools.export_schema(u, HIDDEN, include_data=True)
    assert str(e.value) == refusal["error"]
    assert "CREATE TABLE" in tools.export_schema(u, HIDDEN)["sql"]


def test_global_role_still_caps_export(dbs):
    u = grant(make_user(Role.VIEWER), HIDDEN, Role.EDITOR)   # stored above the cap: never takes effect
    r = web(u).get("/api/schema/export", {"schema": HIDDEN, "data": "1"})
    assert r.status_code == 403 and r.json() == {"error": "exporting data needs the editor role"}
    with pytest.raises(tools.ToolFailure, match="exporting data needs the editor role"):
        tools.export_schema(u, HIDDEN, include_data=True)


def test_editor_grant_exports(dbs):
    u = grant(make_user(Role.EDITOR), HIDDEN, Role.EDITOR)   # nothing on master
    c = web(u)
    r = c.get("/api/schema/export", {"schema": HIDDEN, "data": "1"})
    assert r.status_code == 200 and b"Acme" in body(r)
    r = c.get("/api/schema/tables/t/export.csv", {"schema": HIDDEN})
    assert r.status_code == 200 and b"Acme" in body(r)
    out = tools.export_schema(u, HIDDEN, include_data=True)
    assert "Acme" in out["sql"]
    # master is hidden for this user, data or not
    assert c.get("/api/schema/export", {"data": "1"}).json() == {"error": "Unknown schema 'master'."}


def _fake_backup(settings):
    def fake(schema, label, retention_days):
        p = pathlib.Path(settings.BACKUP_DIR) / f"{schema}-20260101T000000Z-{label}.dump"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"abc")
        return p
    return fake


def test_backup_needs_approver_on_that_database(dbs, monkeypatch, settings):
    monkeypatch.setattr(backup, "backup_schema", _fake_backup(settings))
    u = grant(grant(make_user(Role.APPROVER), "master", Role.APPROVER), HIDDEN, Role.EDITOR)
    refusal = "This needs the approver role on 'crm'; you have editor there."
    with pytest.raises(tools.ToolFailure) as e:
        tools.create_backup(u, HIDDEN)
    assert str(e.value) == refusal
    with pytest.raises(tools.ToolFailure) as e:
        tools.list_backups(u, HIDDEN)
    assert str(e.value) == refusal
    assert tools.create_backup(u)["schema"] == "master"
    assert tools.create_backup(dbs, HIDDEN)["schema"] == HIDDEN   # admin


def test_list_backups_filtered(dbs, settings):
    folder = pathlib.Path(settings.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("master", "crm", "gone"):    # gone: a database dropped since its backup
        (folder / f"{name}-20260101T000000Z-manual.dump").write_bytes(b"abc")

    u = grant(grant(make_user(Role.APPROVER), "master", Role.APPROVER), HIDDEN, Role.VIEWER)
    # (applying the set-up changes also left automatic backups of master and crm)
    assert {b["schema"] for b in tools.list_backups(u)["backups"]} == {"master"}
    assert {b["schema"] for b in backup.list_backups(u)} == {"master"}
    assert tools.list_backups(u, "gone") == {"backups": []}
    assert tools.list_backups(u, MISSING) == {"backups": []}

    star = star_with_deny(make_user(Role.APPROVER))
    assert "crm" not in {b["schema"] for b in tools.list_backups(star)["backups"]}
    assert tools.list_backups(star, HIDDEN) == {"backups": []}

    assert {b["schema"] for b in tools.list_backups(dbs)["backups"]} == {"master", "crm", "gone"}
    assert [b["schema"] for b in tools.list_backups(dbs, "gone")["backups"]] == ["gone"]


def test_user_without_master_gets_hidden_answer_by_default(dbs):
    u = grant(make_user(Role.EDITOR), HIDDEN, Role.EDITOR)
    c = web(u)
    for path in ("/api/schema/tables", "/api/schema/graph", "/api/schema/export", "/api/schema/tables/t",
                 "/api/schema/tables/t/rows", "/api/schema/tables/t/options", "/api/schema/tables/t/export.csv"):
        r = c.get(path)
        body(r)
        assert r.status_code == 400 and r.json() == {"error": "Unknown schema 'master'."}, path
    r = c.post("/api/query", json.dumps({"sql": "SELECT 1"}), content_type="application/json")
    assert r.status_code == 400 and r.json() == {"error": "Unknown schema 'master'."}
    assert status(c.get("/api/schema/tables", {"schema": HIDDEN})) == 200

    with pytest.raises(tools.ToolFailure, match=r"^Unknown schema 'master'\. Call list_schemas"):
        tools.list_tables(u)
    with pytest.raises(tools.ToolFailure, match=r"^Unknown schema 'master'\."):
        tools.run_query(u, "SELECT 1")
    assert {t["name"] for t in tools.list_tables(u, HIDDEN)["tables"]} == {"t", "only_crm"}


# ---- MCP --------------------------------------------------------------------------------------

HEADERS = ["code", "name"]
MCP = {
    "list_tables": lambda u, s: tools.list_tables(u, s),
    "describe_table": lambda u, s: tools.describe_table(u, "t", s),
    "describe_table_only_in_hidden": lambda u, s: tools.describe_table(u, "only_crm", s),
    "query": lambda u, s: tools.run_query(u, "SELECT 1 AS one", schema=s),
    "query_table": lambda u, s: tools.run_query(u, "SELECT * FROM only_crm", schema=s),
    "read_rows": lambda u, s: tools.read_rows(u, "t", s),
    "read_rows_only_in_hidden": lambda u, s: tools.read_rows(u, "only_crm", s),
    "profile_column": lambda u, s: tools.profile_column(u, "t", "name", s),
    "profile_column_only_in_hidden": lambda u, s: tools.profile_column(u, "only_crm", "x", s),
    "lookup_options": lambda u, s: tools.lookup_options(u, "t", schema=s),
    "export_schema": lambda u, s: tools.export_schema(u, s),
    "export_schema_data": lambda u, s: tools.export_schema(u, s, include_data=True),
    "create_backup": lambda u, s: tools.create_backup(u, s),
    "list_backups": lambda u, s: tools.list_backups(u, s),
    "match_data": lambda u, s: tools.match_data(u, s, HEADERS, [["C1", "Acme"]], row_match=True),
    "get_import_recipe": lambda u, s: tools.get_import_recipe(u, s, HEADERS),
}


def _mcp_answer(fn, name):
    try:
        out = ("ok", fn())
    except tools.ToolFailure as exc:
        out = ("fail", str(exc))
    return json.loads(json.dumps(out, default=str).replace(name, "<db>"))


@pytest.mark.parametrize("tool", list(MCP))
def test_hidden_equals_missing_mcp(outsider, tool, monkeypatch, settings):
    monkeypatch.setattr(backup, "backup_schema", _fake_backup(settings))
    folder = pathlib.Path(settings.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{HIDDEN}-20260101T000000Z-manual.dump").write_bytes(b"abc")
    hidden = _mcp_answer(lambda: MCP[tool](outsider, HIDDEN), HIDDEN)
    missing = _mcp_answer(lambda: MCP[tool](outsider, MISSING), MISSING)
    assert hidden == missing
    if tool == "list_backups":
        assert hidden == ["ok", {"backups": []}]
    else:
        assert hidden[0] == "fail" and hidden[1].startswith("Unknown schema '<db>'.")


def test_every_listed_mcp_tool_is_covered():
    listed = {"list_schemas", "list_tables", "describe_table", "query", "read_rows", "profile_column", "lookup_options",
              "export_schema", "create_backup", "list_backups", "match_data", "get_import_recipe"}
    covered = {k.split("_only_in_hidden")[0] for k in MCP} | {"list_schemas"}   # list_schemas: test above
    assert listed <= covered
    assert all(t in tools.IMPL for t in listed)


@pytest.mark.parametrize("tool", [k for k in MCP if "only_in_hidden" not in k and k not in ("query_table", "create_backup")])
def test_mcp_reads_with_a_grant(dbs, tool, monkeypatch, settings):
    monkeypatch.setattr(backup, "backup_schema", _fake_backup(settings))
    u = grant(make_user(Role.APPROVER), HIDDEN, Role.APPROVER)
    assert _mcp_answer(lambda: MCP[tool](u, HIDDEN), HIDDEN)[0] == "ok"


def test_unknown_schema_text_is_unchanged_for_admins(dbs):
    with pytest.raises(tools.ToolFailure) as e:
        tools.list_tables(dbs, MISSING)
    assert str(e.value) == f"{introspect.unknown_schema_message(MISSING)} Call list_schemas to see the databases."
