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
    assert ed.get("/api/duplicates").json()["groups"] == []  # absent = master


def test_search_schema_filter(two, settings, monkeypatch):
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [fake_vector(t) for t in texts])
    indexer.reindex()
    c = client(Role.VIEWER)
    allr = c.get("/api/search?q=t&k=20&schema=*").json()["results"]
    assert {r["schema"] for r in allr} == {"master", "crm"}
    assert {r["schema"] for r in c.get("/api/search?q=t&k=20").json()["results"]} == {"master"}
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


# --- one rule for the schema argument: absent = master, present must be a non-empty managed string ---

BAD_VALUES = ["", 0, False, [], {}, 5, ["a", "b"]]
OPS = [{"op": "create_table", "name": "extra", "columns": [{"name": "x", "type": "text"}]}]


def _post(c, url, body):
    return c.post(url, json.dumps(body), content_type="application/json")


@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
def test_bad_schema_values_in_body_are_400(two, bad):
    ed = client(Role.EDITOR)
    for url, body in [("/api/query", {"sql": "select 1"}), ("/api/duplicates/scan", {"table": "t"}),
                      ("/api/changes", {"operations": OPS})]:
        r = _post(ed, url, {**body, "schema": bad})
        assert r.status_code == 400, (url, bad, r.status_code)
        assert r.json()["error"]
    assert not Change.objects.filter(requester__email="editor@x.com").exists()


@pytest.mark.parametrize("qs", ["schema=", "schema=a&schema=b", "schema=crm&schema=master"])
def test_bad_schema_values_in_query_are_400(two, qs, opened):
    ed = client(Role.EDITOR)
    for r in [
        ed.get(f"/api/schema/export?{qs}"), ed.get(f"/api/schema/tables?{qs}"), ed.get(f"/api/search?q=t&{qs}"),
        ed.get(f"/api/duplicates?{qs}"), ed.get(f"/api/changes?{qs}"),
        _post(ed, f"/api/query?{qs}", {"sql": "select 1"}), _post(ed, f"/api/duplicates/scan?{qs}", {"table": "t"}),
        _post(ed, f"/api/changes?{qs}", {"operations": OPS}),
    ]:
        assert r.status_code == 400, (r.request["PATH_INFO"], qs, r.status_code)


def test_export_empty_schema_is_400_not_master(two, opened):
    assert client(Role.VIEWER).get("/api/schema/export?schema=").status_code == 400
    r = client(Role.EDITOR).get("/api/schema/export")  # absent = master
    assert r.status_code == 200
    assert "only_crm" not in b"".join(r.streaming_content).decode()


def test_absent_schema_is_master_and_query_body_conflict_is_400(two):
    ed = client(Role.EDITOR)
    assert _post(ed, "/api/query", {"sql": "select code from t"}).json()["rows"] == [["M1"]]
    assert _post(ed, "/api/query?schema=crm", {"sql": "select code from t", "schema": "crm"}).json()["rows"] == [["C1"], ["C2"]]
    assert _post(ed, "/api/query?schema=crm", {"sql": "select 1", "schema": "master"}).status_code == 400
    assert _post(ed, "/api/changes?schema=crm", {"operations": OPS, "schema": "master"}).status_code == 400
    r = _post(ed, "/api/changes", {"operations": OPS})
    assert Change.objects.get(pk=r.json()["id"]).schema == "master"


def test_schema_ops_land_in_master_even_with_a_schema(two):
    u = make_user(Role.EDITOR)
    op = [{"op": "create_schema", "name": "hr"}]
    assert Change.objects.get(pk=tools.propose_change(u, op, schema="crm")["id"]).schema == "master"
    assert service.propose(u, op, source="web", schema="crm").schema == "master"
    for bad in ("", 5, ["a"]):
        with pytest.raises(tools.ToolFailure):
            tools.propose_change(u, OPS, schema=bad)


# --- final wave ---

def test_duplicates_and_search_default_to_master_and_star_means_all(two, settings, monkeypatch):
    monkeypatch.setattr(dup, "_engine", lambda: FakeEngine)
    apply(two, [{"op": "insert_rows", "table": "t", "rows": [{"code": "M2", "name": "Acme One"}, {"code": "M3", "name": "Acme Two"}]}])
    ed = client(Role.EDITOR)
    for schema in ("master", "crm"):
        assert _post(ed, f"/api/duplicates/scan?schema={schema}", {"table": "t"}).json()["groups"] == 1
    assert {g["schema"] for g in ed.get("/api/duplicates").json()["groups"]} == {"master"}
    assert {g["schema"] for g in ed.get("/api/duplicates?schema=*").json()["groups"]} == {"master", "crm"}
    assert {g["schema"] for g in ed.get("/api/duplicates?schema=crm").json()["groups"]} == {"crm"}
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [fake_vector(t) for t in texts])
    indexer.reindex()
    assert {r["schema"] for r in ed.get("/api/search?q=t&k=20").json()["results"]} == {"master"}
    assert {r["schema"] for r in ed.get("/api/search?q=t&k=20&schema=*").json()["results"]} == {"master", "crm"}
    # `*` is only for these two endpoints
    assert ed.get("/api/schema/tables?schema=*").status_code == 400
    assert ed.get("/api/schema/export?schema=*").status_code == 400
    assert _post(ed, "/api/query?schema=*", {"sql": "select 1"}).status_code == 400
    assert ed.get("/api/duplicates?schema=*&schema=crm").status_code == 400
    u = make_user(Role.VIEWER)
    assert {r["schema"] for r in tools.search_schema(u, "t", 20)["results"]} == {"master"}
    assert {r["schema"] for r in tools.search_schema(u, "t", 20, "*")["results"]} == {"master", "crm"}


SCRIPT = "CREATE TABLE public.thing (id integer PRIMARY KEY, name text);\nINSERT INTO public.thing VALUES (1, 'a');\n"


@pytest.fixture
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


def test_script_validate_api_takes_schema(two, script_dir):
    from django.core.files.uploadedfile import SimpleUploadedFile
    ed = client(Role.EDITOR)
    sid = ed.post("/api/scripts", {"file": SimpleUploadedFile("t.sql", SCRIPT.encode())}).json()["script_id"]
    r = _post(ed, f"/api/scripts/{sid}/validate", {"schema": "crm"})
    assert r.status_code == 200 and r.json()["schema"] == "crm"
    assert "thing" not in {t["name"] for t in introspect.list_tables("master")}
    assert "thing" not in {t["name"] for t in introspect.list_tables("crm")}  # dry run rolled back
    assert _post(ed, f"/api/scripts/{sid}/validate", {}).json()["schema"] == "master"
    for bad in ("nope", "public", "singularity", "", 5, ["a"]):
        assert _post(ed, f"/api/scripts/{sid}/validate", {"schema": bad}).status_code == 400, bad


def test_script_mcp_takes_schema(two, script_dir):
    u = make_user(Role.EDITOR)
    out = tools.submit_script(u, "t.sql", SCRIPT, schema="crm")
    assert out["report"]["schema"] == "crm"
    assert Change.objects.get(pk=out["change_id"]).schema == "crm"
    assert tools.validate_script(u, out["script_id"], schema="master")["report"]["schema"] == "master"
    assert "thing" not in {t["name"] for t in introspect.list_tables("master")}
    for bad in ("nope", "public", ""):
        with pytest.raises(tools.ToolFailure):
            tools.submit_script(u, "u.sql", SCRIPT, schema=bad)
        with pytest.raises(tools.ToolFailure):
            tools.validate_script(u, out["script_id"], schema=bad)
    props = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in toolspecs.specs_for(u)}
    assert "schema" in props["submit_script"] and "schema" in props["validate_script"]


def test_backups_only_for_rename_schema(two, monkeypatch):
    from approvals import service as svc
    made = []
    monkeypatch.setattr(svc, "backup_schema", lambda schema, label, days: made.append(schema) or __import__("pathlib").Path("x.sql"))
    apply(two, [{"op": "create_schema", "name": "tmp"}])
    assert made == []
    apply(two, [{"op": "rename_schema", "schema": "tmp", "new_name": "tmp2"}])
    assert made == ["tmp"]
    apply(two, [{"op": "drop_schema", "schema": "tmp2", "reason": "unused"}])
    assert made == ["tmp"]


def test_create_schema_comment_is_in_the_rendered_sql_and_applied(two):
    from gate import sqlgen
    op = {"op": "create_schema", "name": "hr", "comment": "people's data"}
    rendered = "\n".join(sqlgen.render([op], {}, "me@x.com"))
    assert "COMMENT ON SCHEMA" in rendered and "people''s data" in rendered
    apply(two, [op])
    assert {s["name"]: s["comment"] for s in introspect.managed_schemas()}["hr"] == "people's data"


def test_rename_schema_moves_duplicate_groups_and_triggers_reindex(two, monkeypatch, django_capture_on_commit_callbacks):
    from duplicates.models import DuplicateGroup
    from search import indexer as idx
    monkeypatch.setattr(dup, "_engine", lambda: FakeEngine)
    dup.scan("t", two, schema="crm")
    old = DuplicateGroup.objects.get(schema="crm")
    called = []
    monkeypatch.setattr(idx, "schedule_reindex", lambda: called.append(1))
    with django_capture_on_commit_callbacks(execute=True):
        apply(two, [{"op": "rename_schema", "schema": "crm", "new_name": "sales"}])
    g = DuplicateGroup.objects.get()
    assert g.pk == old.pk and g.schema == "sales" and g.key == dup.group_key("t", g.member_ids, "sales") != old.key
    assert called
    assert dup.scan("t", two, schema="sales")["groups"] == 1 and DuplicateGroup.objects.count() == 1  # same group, not a new one
