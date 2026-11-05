import json

import pytest
from django.test import Client

from accounts.models import Role, User
from approvals.models import Change

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


def login(role, email=None):
    u = User.objects.create(username=email or f"{role}@example.com", email=email or f"{role}@example.com", role=role)
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture
def viewer():
    return login(Role.VIEWER)


@pytest.fixture
def editor():
    return login(Role.EDITOR)


@pytest.fixture
def approver():
    return login(Role.APPROVER)


def post(c, url, data=None):
    return c.post(url, data=json.dumps(data or {}), content_type="application/json")


def patch(c, url, data):
    return c.patch(url, data=json.dumps(data), content_type="application/json")


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


CREATE = {"operations": [{"op": "create_table", "name": "customer", "columns": [col("customerCode", unique=True), col("name")]}]}


# ---- auth and roles --------------------------------------------------------

@pytest.mark.parametrize("method,url", [
    ("get", "/api/changes"), ("get", "/api/queue"), ("get", "/api/rules"), ("get", "/api/schema/tables"),
    ("get", "/api/audit"), ("post", "/api/query"), ("get", "/api/mcp-tokens"),
])
def test_everything_requires_sign_in(client, method, url):
    assert getattr(client, method)(url).status_code == 401


def test_viewer_can_read_but_not_propose(viewer):
    for url in ("/api/changes", "/api/rules", "/api/schema/tables", "/api/audit"):
        assert viewer.get(url).status_code == 200
    assert post(viewer, "/api/changes", CREATE).status_code == 403
    assert viewer.get("/api/queue").status_code == 403
    assert viewer.get("/api/mcp-tokens").status_code == 403


def test_wrong_method_and_bad_body(editor):
    assert editor.delete("/api/changes").status_code == 405
    assert editor.post("/api/changes", data="{not json", content_type="application/json").status_code == 400
    assert editor.post("/api/changes", data="[1]", content_type="application/json").status_code == 400


def test_csrf_is_enforced_for_browsers():
    u = User.objects.create(username="e@example.com", email="e@example.com", role=Role.EDITOR)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(u)
    assert post(strict, "/api/changes", CREATE).status_code == 403
    assert Change.objects.count() == 0


# ---- change flow over HTTP --------------------------------------------------

def test_propose_review_approve_flow(editor, approver):
    r = post(editor, "/api/changes", {**CREATE, "prompt": "add customers"})
    assert r.status_code == 201
    c = r.json()
    assert c["status"] == "pending" and c["kind"] == "schema" and c["prompt"] == "add customers"
    assert c["diff"][0].startswith("+ create table customer") and c["sql"]

    # the editor cannot approve their own schema change
    r = post(editor, f"/api/changes/{c['id']}/approve")
    assert r.status_code == 403 and r.json()["code"] == "forbidden"

    # the approver sees it in the queue, then approves it
    q = approver.get("/api/queue").json()["changes"]
    assert [x["id"] for x in q] == [c["id"]]
    r = post(approver, f"/api/changes/{c['id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "applied"

    assert [t["name"] for t in approver.get("/api/schema/tables").json()["tables"]] == ["customer"]
    d = approver.get("/api/schema/tables/customer").json()
    assert d["columns"][0]["name"] == "id"
    assert approver.get("/api/schema/tables/nope").status_code == 404
    assert approver.get(f"/api/changes/{c['id']}").json()["status"] == "applied"


def test_gate_rejection_is_a_normal_201_with_reasons(editor):
    r = post(editor, "/api/changes", {"operations": [{"op": "create_table", "name": "Bad_Name", "columns": [col("x")]}]})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "rejected"
    assert body["check"]["violations"][0]["suggestion"] == "badName"


def test_callers_cannot_forge_the_source(editor):
    assert post(editor, "/api/changes", {**CREATE, "source": "revert"}).status_code == 400
    assert post(editor, "/api/changes", {**CREATE, "source": "mcp"}).status_code == 400
    assert post(editor, "/api/changes", {**CREATE, "source": "grid"}).status_code == 201


def test_error_codes(editor, approver):
    assert post(approver, "/api/changes/999/approve").status_code == 404
    c = post(approver, "/api/changes", CREATE).json()
    post(approver, f"/api/changes/{c['id']}/approve")
    assert post(approver, f"/api/changes/{c['id']}/approve").status_code == 409      # already applied
    assert post(approver, f"/api/changes/{c['id']}/explode").status_code == 404
    assert post(editor, f"/api/changes/{c['id']}/revert").status_code == 403


def test_reject_with_note_and_revert(editor, approver):
    c = post(editor, "/api/changes", CREATE).json()
    r = post(approver, f"/api/changes/{c['id']}/reject", {"note": "duplicate of another table"})
    assert r.json()["status"] == "rejected" and r.json()["decisionNote"] == "duplicate of another table"

    c2 = post(approver, "/api/changes", CREATE).json()
    post(approver, f"/api/changes/{c2['id']}/approve")
    rev = post(approver, f"/api/changes/{c2['id']}/revert")
    assert rev.status_code == 200 and rev.json()["status"] == "applied" and rev.json()["source"] == "revert"
    assert approver.get("/api/schema/tables").json()["tables"] == []


def test_list_filters(editor, approver):
    post(editor, "/api/changes", CREATE)
    post(approver, "/api/changes", {"operations": [{"op": "create_table", "name": "Bad_Name", "columns": [col("x")]}]})
    assert len(approver.get("/api/changes").json()["changes"]) == 2
    assert len(approver.get("/api/changes?status=pending").json()["changes"]) == 1
    assert len(editor.get("/api/changes?mine=1").json()["changes"]) == 1
    assert len(approver.get("/api/changes?limit=1").json()["changes"]) == 1
    assert "operations" not in approver.get("/api/changes").json()["changes"][0]       # list view is light


# ---- query ------------------------------------------------------------------

def test_query_endpoint(approver, viewer):
    c = post(approver, "/api/changes", CREATE).json()
    post(approver, f"/api/changes/{c['id']}/approve")
    ins = post(approver, "/api/changes", {"operations": [
        {"op": "insert_rows", "table": "customer", "rows": [{"customerCode": "C1", "name": "Acme"}]}]}).json()
    post(approver, f"/api/changes/{ins['id']}/approve")
    r = post(viewer, "/api/query", {"sql": "SELECT \"customerCode\", name FROM customer"})
    assert r.status_code == 200 and r.json()["rows"] == [["C1", "Acme"]]
    assert post(viewer, "/api/query", {"sql": "DROP TABLE customer"}).status_code == 400
    assert post(viewer, "/api/query", {"sql": "SELECT 1", "limit": "many"}).status_code == 400


def test_query_limit_is_capped_by_config(approver, monkeypatch):
    monkeypatch.setattr("schema.views.get_config", lambda: {"limits": {"maxQueryRows": 2, "queryTimeoutMs": 2000}})
    r = post(approver, "/api/query", {"sql": "SELECT generate_series(1, 10)", "limit": 100})
    assert len(r.json()["rows"]) == 2 and r.json()["truncated"] is True


# ---- rules ------------------------------------------------------------------

def test_rules_read_for_all_write_for_approvers(viewer, editor, approver):
    rules = viewer.get("/api/rules").json()["rules"]
    assert len(rules) == 10 and rules[0]["name"] == "tableNaming"
    rid = rules[0]["id"]
    assert patch(editor, f"/api/rules/{rid}", {"enabled": False}).status_code == 403
    r = patch(approver, f"/api/rules/{rid}", {"params": {**rules[0]["params"], "style": "snake_case"}})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert [v["version"] for v in viewer.get(f"/api/rules/{rid}/versions").json()["versions"]] == [2, 1]
    back = post(approver, f"/api/rules/{rid}/rollback", {"version": 1})
    assert back.json()["params"]["style"] == "camelCase" and back.json()["version"] == 3
    assert post(editor, f"/api/rules/{rid}/rollback", {"version": 1}).status_code == 403
    assert post(approver, f"/api/rules/{rid}/rollback", {"version": 99}).status_code == 400


def test_rule_validation_errors_are_listed(approver):
    r = post(approver, "/api/rules", {"name": "x", "type": "naming", "params": {"target": "table", "style": "kebab"}})
    assert r.status_code == 400 and any("style" in e for e in r.json()["errors"])
    ok = post(approver, "/api/rules", {"name": "noDropIndex", "type": "forbidden",
                                       "params": {"ops": ["drop_index"]}, "severity": "warn"})
    assert ok.status_code == 201 and ok.json()["name"] == "noDropIndex"
    bad = patch(approver, f"/api/rules/{ok.json()['id']}", {"name": "renamed"})
    assert bad.status_code == 400
    assert approver.get("/api/rules/9999").status_code == 404


def test_rule_change_takes_effect_for_the_next_proposal(editor, approver):
    ops = {"operations": [{"op": "create_table", "name": "supplier_contact", "columns": [col("contact_code", unique=True)]}]}
    assert post(editor, "/api/changes", ops).json()["status"] == "rejected"          # camelCase is the default
    for r in approver.get("/api/rules").json()["rules"]:
        if r["name"] in ("tableNaming", "columnNaming", "foreignKeyNaming"):
            patch(approver, f"/api/rules/{r['id']}", {"params": {**r["params"], "style": "snake_case"}})
    assert post(editor, "/api/changes", ops).json()["status"] == "pending"


# ---- tokens and audit ------------------------------------------------------

def test_token_lifecycle(editor):
    r = post(editor, "/api/mcp-tokens", {"name": "cursor"})
    assert r.status_code == 201 and r.json()["token"].startswith("sgl_")
    tid = r.json()["id"]
    listed = editor.get("/api/mcp-tokens").json()["tokens"]
    assert [t["name"] for t in listed] == ["cursor"] and "token" not in listed[0]       # never shown again
    assert post(editor, "/api/mcp-tokens", {"name": ""}).status_code == 400
    assert editor.delete(f"/api/mcp-tokens/{tid}").status_code == 200
    assert editor.get("/api/mcp-tokens").json()["tokens"] == []
    assert editor.delete(f"/api/mcp-tokens/{tid}").status_code == 404


def test_audit_endpoint(editor, approver, viewer):
    c = post(editor, "/api/changes", CREATE).json()
    post(approver, f"/api/changes/{c['id']}/approve")
    entries = viewer.get("/api/audit").json()["entries"]
    assert {"change.proposed", "change.approved", "change.applied"} <= {e["action"] for e in entries}
    only = viewer.get("/api/audit?action=change.applied").json()["entries"]
    assert len(only) == 1 and only[0]["actor"] == "approver@example.com"


# ---- schema graph (2D diagram data) -------------------------------------------

def test_graph_has_columns_keys_and_foreign_keys(approver):
    ops = [
        {"op": "create_table", "name": "customer", "columns": [col("name")]},
        {"op": "create_table", "name": "orderHeader", "columns": [
            col("customerId", "bigint", references={"table": "customer"})]},
    ]
    ch = post(approver, "/api/changes", {"operations": ops}).json()
    assert post(approver, f"/api/changes/{ch['id']}/approve").json()["status"] == "applied"
    g = approver.get("/api/schema/graph").json()
    by = {t["name"]: t for t in g["tables"]}
    assert set(by) == {"customer", "orderHeader"}
    assert {c["name"] for c in by["customer"]["columns"] if c["primaryKey"]} == {"id"}
    assert g["relations"] == [{"from": {"table": "orderHeader", "column": "customerId"},
                               "to": {"table": "customer", "column": "id"}}]


def test_graph_is_readable_by_viewers_but_not_anonymous(viewer):
    assert viewer.get("/api/schema/graph").json() == {"tables": [], "relations": []}
    assert Client().get("/api/schema/graph").status_code == 401
