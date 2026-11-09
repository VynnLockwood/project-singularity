"""Task 8: the schema parameter on the API, the schemas endpoint, MCP tools and chat tools."""
import json

import pytest
from django.test import Client

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from chat import orchestrator, toolspecs
from duplicates import service as dup
from mcp_server import tools
from schema import introspect
from search import embedder, indexer

from . import pgsetup
from .test_schemas_consumers import NAMES, TABLE, FakeEngine, fake_vector

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect("singularity_master_rw", master_db) as rw:
        for (s,) in rw.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            rw.execute(f'DROP SCHEMA "{s}" CASCADE')


def make_user(role):
    return User.objects.create(username=f"{role}@x.com", email=f"{role}@x.com", role=role)


def client(role):
    c = Client()
    c.force_login(make_user(role))
    return c


def apply(user, ops, schema="master"):
    ch = service.propose(user, ops, source="web", schema=schema)
    assert ch.status == "pending", ch.check_result
    return service.approve(user, ch.pk)


@pytest.fixture
def two():
    a = make_user(Role.APPROVER)
    apply(a, [{"op": "create_schema", "name": "crm", "comment": "customers"}])
    for schema in ("master", "crm"):
        apply(a, [TABLE], schema)
    apply(a, [{"op": "create_table", "name": "only_crm", "columns": [{"name": "x", "type": "text"}]}], "crm")
    apply(a, [{"op": "insert_rows", "table": "t", "rows": [{"code": "M1", "name": "Alpha"}]}])
    apply(a, [{"op": "insert_rows", "table": "t", "rows": [
        {"code": "C1", "name": NAMES[0]}, {"code": "C2", "name": NAMES[1]}]}], "crm")
    return a


def names(resp, key="tables"):
    return {t["name"] for t in resp.json()[key]}


def test_schemas_endpoint_lists_managed_only(two):
    r = client(Role.VIEWER).get("/api/schema/schemas")
    assert r.status_code == 200
    by = {s["name"]: s for s in r.json()["schemas"]}
    assert list(by) == ["master", "crm"]
    assert by["master"]["tables"] == 1 and by["crm"]["tables"] == 2 and by["crm"]["comment"] == "customers"
    assert not {"singularity", "public"} & set(by)


def test_schemas_endpoint_auth():
    assert Client().get("/api/schema/schemas").status_code == 401
    assert client(Role.VIEWER).post("/api/schema/schemas").status_code == 405


def test_schema_param_reads_crm(two):
    c = client(Role.VIEWER)
    assert names(c.get("/api/schema/tables")) == {"t"}
    assert names(c.get("/api/schema/tables?schema=crm")) == {"t", "only_crm"}
    assert names(c.get("/api/schema/graph?schema=crm"), "tables") >= {"t", "only_crm"}
    assert c.get("/api/schema/tables/only_crm").status_code == 404
    assert c.get("/api/schema/tables/only_crm?schema=crm").json()["name"] == "only_crm"
    assert {r["code"] for r in c.get("/api/schema/tables/t/rows?schema=crm").json()["rows"]} == {"C1", "C2"}
    assert {r["code"] for r in c.get("/api/schema/tables/t/rows").json()["rows"]} == {"M1"}
    assert len(c.get("/api/schema/tables/t/options?schema=crm").json()["options"]) == 2
    q = lambda url: c.post(url, json.dumps({"sql": "select code from t order by 1"}), content_type="application/json")
    assert q("/api/query").json()["rows"] == [["M1"]]
    assert q("/api/query?schema=crm").json()["rows"] == [["C1"], ["C2"]]


BAD = ["public", "singularity", "nope"]


@pytest.mark.parametrize("bad", BAD)
def test_unmanaged_schema_is_400_everywhere(two, bad, monkeypatch):
    monkeypatch.setattr(dup, "_engine", lambda: FakeEngine)
    c = ed = client(Role.EDITOR)
    post = lambda cl, url, body: cl.post(url, json.dumps(body), content_type="application/json")
    responses = [
        c.get(f"/api/schema/tables?schema={bad}"),
        c.get(f"/api/schema/graph?schema={bad}"),
        c.get(f"/api/schema/tables/t?schema={bad}"),
        c.get(f"/api/schema/tables/t/rows?schema={bad}"),
        c.get(f"/api/schema/tables/t/options?schema={bad}"),
        post(c, f"/api/query?schema={bad}", {"sql": "select 1"}),
        post(c, "/api/query", {"sql": "select 1", "schema": bad}),
        c.get(f"/api/search?q=t&schema={bad}"),
        c.get(f"/api/duplicates?schema={bad}"),
        post(ed, f"/api/duplicates/scan?schema={bad}", {"table": "t"}),
        post(ed, "/api/duplicates/scan", {"table": "t", "schema": bad}),
    ]
    for r in responses:
        assert r.status_code == 400, (r.request["PATH_INFO"], r.status_code)
        assert r.json()["error"]


def test_unauthenticated_still_rejected(two):
    c = Client()
    for url in ["/api/schema/tables?schema=crm", "/api/schema/graph?schema=crm", "/api/schema/tables/t?schema=crm",
                "/api/schema/tables/t/rows?schema=crm", "/api/search?q=t&schema=crm", "/api/duplicates?schema=crm"]:
        assert c.get(url).status_code == 401, url
    assert c.post("/api/query?schema=crm", "{}", content_type="application/json").status_code == 401
    assert c.post("/api/duplicates/scan?schema=crm", "{}", content_type="application/json").status_code == 401
    assert client(Role.VIEWER).post("/api/duplicates/scan?schema=crm", "{}", content_type="application/json").status_code == 403


def test_duplicates_scan_and_list_take_schema(two, monkeypatch):
    monkeypatch.setattr(dup, "_engine", lambda: FakeEngine)
    ed = client(Role.EDITOR)
    r = ed.post("/api/duplicates/scan?schema=crm", json.dumps({"table": "t"}), content_type="application/json")
    assert r.status_code == 200 and r.json()["schema"] == "crm" and r.json()["groups"] == 1
    r = ed.post("/api/duplicates/scan", json.dumps({"table": "t"}), content_type="application/json")
    assert r.json()["schema"] == "master" and r.json()["groups"] == 0
    g = ed.get("/api/duplicates?schema=crm").json()
    assert [x["schema"] for x in g["groups"]] == ["crm"]
    assert ed.get("/api/duplicates?schema=master").json()["groups"] == []


def test_search_schema_filter(two, settings, monkeypatch):
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [fake_vector(t) for t in texts])
    indexer.reindex()
    c = client(Role.VIEWER)
    allr = c.get("/api/search?q=t&k=20").json()["results"]
    assert {r["schema"] for r in allr} == {"master", "crm"}
    only = c.get("/api/search?q=t&k=20&schema=crm").json()["results"]
    assert only and {r["schema"] for r in only} == {"crm"}


def test_propose_with_schema_in_body(two):
    c = client(Role.EDITOR)
    ops = [{"op": "create_table", "name": "extra", "columns": [{"name": "x", "type": "text"}]}]
    r = c.post("/api/changes", json.dumps({"operations": ops, "schema": "crm"}), content_type="application/json")
    assert r.status_code == 201 and Change.objects.get(pk=r.json()["id"]).schema == "crm"
    r = c.post("/api/changes", json.dumps({"operations": ops, "schema": "nope"}), content_type="application/json")
    assert r.status_code == 400
    assert c.get("/api/changes?schema=crm").json()["changes"][0]["schema"] == "crm"


@pytest.fixture
def opened(monkeypatch):
    from schema import views as schema_views
    made, real = [], schema_views.open_export

    def spy(*a, **k):
        made.append(real(*a, **k))
        return made[-1]

    monkeypatch.setattr(schema_views, "open_export", spy)
    yield made
    for ex in made:
        ex.close()


def test_export_still_works_for_crm(two, opened):
    ed = client(Role.EDITOR)
    r = ed.get("/api/schema/export?schema=crm&data=1")
    assert r.status_code == 200
    body = b"".join(r.streaming_content).decode()
    assert "only_crm" in body and "C1" in body
    v = client(Role.VIEWER)
    r = v.get("/api/schema/export?schema=crm")
    assert r.status_code == 200
    b"".join(r.streaming_content)
    assert v.get("/api/schema/export?schema=crm&data=1").status_code == 403
    assert v.get("/api/schema/export?schema=public").status_code == 400


# --- MCP tools ---

def test_mcp_tools_are_schema_aware(two):
    u = make_user(Role.VIEWER)
    assert [s["name"] for s in tools.list_schemas(u)["schemas"]] == ["master", "crm"]
    assert {t["name"] for t in tools.list_tables(u)["tables"]} == {"t"}
    assert {t["name"] for t in tools.list_tables(u, "crm")["tables"]} == {"t", "only_crm"}
    assert tools.describe_table(u, "only_crm", "crm")["name"] == "only_crm"
    with pytest.raises(tools.ToolFailure):
        tools.describe_table(u, "only_crm")
    assert tools.run_query(u, "select code from t", schema="crm")["rows"] == [["C1"], ["C2"]]
    for bad in BAD:
        with pytest.raises(tools.ToolFailure):
            tools.list_tables(u, bad)
        with pytest.raises(tools.ToolFailure):
            tools.describe_table(u, "t", bad)
        with pytest.raises(tools.ToolFailure):
            tools.run_query(u, "select 1", schema=bad)


def test_mcp_list_schemas_is_viewer_level():
    assert tools.TOOL_ROLES["list_schemas"] == "viewer" and "list_schemas" in tools.IMPL
    assert tools.allowed(make_user(Role.VIEWER), "list_schemas")


def test_mcp_propose_change_forwards_schema(two):
    u = make_user(Role.EDITOR)
    ops = [{"op": "create_table", "name": "extra", "columns": [{"name": "x", "type": "text"}]}]
    out = tools.propose_change(u, ops, schema="crm")
    assert Change.objects.get(pk=out["id"]).schema == "crm"
    assert Change.objects.get(pk=tools.propose_change(u, ops)["id"]).schema == "master"
    with pytest.raises(tools.ToolFailure):
        tools.propose_change(u, ops, schema="nope")


# --- chat tools ---

def test_chat_toolspecs_include_schema_and_list_schemas():
    specs = {s["function"]["name"]: s["function"] for s in toolspecs.specs_for(make_user(Role.EDITOR))}
    assert "list_schemas" in specs
    for name in ("list_tables", "describe_table", "query", "propose_change"):
        assert specs[name]["parameters"]["properties"]["schema"]["type"] == "string"
        assert "schema" not in specs[name]["parameters"]["required"]
    d = specs["propose_change"]["description"]
    assert "create_schema{name,comment?}" in d and "rename_schema{schema,new_name}" in d and "drop_schema{schema,reason}" in d
    assert "Schema ops go alone, in their own change; a 'database' is a Postgres schema" in d


def test_chat_dispatch_forwards_schema(two):
    u = make_user(Role.EDITOR)
    run = lambda name, args: orchestrator.run_tool(u, None, name, json.dumps(args))
    assert [s["name"] for s in run("list_schemas", {})["schemas"]] == ["master", "crm"]
    assert {t["name"] for t in run("list_tables", {"schema": "crm"})["tables"]} == {"t", "only_crm"}
    assert run("query", {"sql": "select code from t", "schema": "crm"})["rows"] == [["C1"], ["C2"]]
    assert "error" in run("list_tables", {"schema": "nope"})
    conv = type("C", (), {"pk": None})()
    ops = [{"op": "create_table", "name": "extra", "columns": [{"name": "x", "type": "text"}]}]
    out = orchestrator.run_tool(u, conv, "propose_change", json.dumps({"operations": ops, "schema": "crm"}))
    assert Change.objects.get(pk=out["id"]).schema == "crm"
