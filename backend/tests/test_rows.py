import json

import pytest
from django.test import Client

from accounts.models import Role, User
from approvals import service
from schema import rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def apply(user, ops, source="web"):
    ch = service.propose(user, ops, source=source)
    assert ch.status == "pending", ch.check_result
    return service.approve(user, ch.pk)


@pytest.fixture
def data(approver):
    apply(approver, [
        {"op": "create_table", "name": "customer", "columns": [
            {"name": "customerCode", "type": "text", "unique": True}, {"name": "name", "type": "text"},
            {"name": "creditLimit", "type": "numeric(12,2)", "nullable": True}]},
        {"op": "create_table", "name": "orderHeader", "columns": [
            {"name": "customerId", "type": "bigint", "references": {"table": "customer"}}]},
    ])
    names = ["Acme", "Beta 100%", "Gamma_Co", "สมชาย การค้า", "Delta"]
    apply(approver, [{"op": "insert_rows", "table": "customer", "rows": [
        {"customerCode": f"C{i:03d}", "name": n, "creditLimit": i * 10} for i, n in enumerate(names, 1)]}])


def api(role=Role.VIEWER):
    u, _ = User.objects.get_or_create(username=f"{role}@z.com", defaults={"email": f"{role}@z.com", "role": role})
    c = Client()
    c.force_login(u)
    return c


def get(c, table, **params):
    if "filters" in params:
        params["filters"] = json.dumps(params["filters"])
    return c.get(f"/api/schema/tables/{table}/rows", params)


def test_pages_and_totals(data):
    r = get(api(), "customer", limit=2, offset=0).json()
    assert r["total"] == 5 and len(r["rows"]) == 2 and [x["customerCode"] for x in r["rows"]] == ["C001", "C002"]
    r = get(api(), "customer", limit=2, offset=4).json()
    assert [x["customerCode"] for x in r["rows"]] == ["C005"]


def test_sorting_both_ways_with_stable_ties(data):
    asc = [x["name"] for x in get(api(), "customer", sort="name").json()["rows"]]
    desc = [x["name"] for x in get(api(), "customer", sort="name", dir="desc").json()["rows"]]
    assert asc == sorted(asc) and desc == asc[::-1]


def test_filter_is_a_contains_match_and_wildcards_are_literal(data):
    names = lambda f: [x["name"] for x in get(api(), "customer", filters=f).json()["rows"]]
    assert names({"name": "ACME"}) == ["Acme"]
    assert names({"name": "%"}) == ["Beta 100%"]       # not "match everything"
    assert names({"name": "_"}) == ["Gamma_Co"]
    assert names({"name": "การค้า"}) == ["สมชาย การค้า"]
    assert names({"customerCode": "C00", "name": "a"}) != []   # filters combine with AND
    assert get(api(), "customer", filters={"creditLimit": "30"}).json()["total"] == 1   # non-text columns too


def test_inactive_rows_hidden_unless_asked(data, approver):
    apply(approver, [{"op": "deactivate_rows", "table": "customer", "where": {"customerCode": "C001"}}])
    assert get(api(), "customer").json()["total"] == 4
    assert get(api(), "customer", inactive="1").json()["total"] == 5


def test_column_metadata(data):
    cols = {c["name"]: c for c in get(api(), "orderHeader").json()["columns"]}
    assert cols["customerId"]["references"] == {"table": "customer", "column": "id"}
    assert cols["customerId"]["readOnly"] is False
    assert all(cols[n]["readOnly"] for n in ("id", "createdAt", "updatedAt", "createdBy", "isActive"))


@pytest.mark.parametrize("params", [
    {"sort": "nope"}, {"sort": 'id; DROP TABLE master.customer'}, {"dir": "sideways"},
    {"filters": {"nope": "x"}}, {"limit": "abc"}, {"offset": "-"},
])
def test_bad_input_is_a_400_and_changes_nothing(data, params):
    assert get(api(), "customer", **params).status_code == 400
    assert get(api(), "customer").json()["total"] == 5


def test_limit_is_capped_and_offset_cannot_go_negative(data):
    assert get(api(), "customer", limit=10_000).json()["limit"] == rows.MAX_PAGE
    assert get(api(), "customer", offset=-5).json()["offset"] == 0


def test_unknown_table_and_auth(data):
    assert get(api(), "ghost").status_code == 404
    assert get(api(), "pg_catalog.pg_class").status_code in (404, 400)
    assert Client().get("/api/schema/tables/customer/rows").status_code == 401


def test_numbers_survive_as_json(data):
    r = get(api(), "customer", limit=1).json()["rows"][0]
    assert r["creditLimit"] == "10.00" and isinstance(r["id"], int)   # numeric stays exact, as text


# ---- reference dropdown options --------------------------------------------------

def test_options_use_the_name_column_and_search(data):
    r = api().get("/api/schema/tables/customer/options").json()
    assert r["labelColumn"] == "name" and len(r["options"]) == 5 and r["truncated"] is False
    q = api().get("/api/schema/tables/customer/options", {"q": "delta"}).json()["options"]
    assert [o["label"] for o in q] == ["Delta"]
    assert api().get("/api/schema/tables/customer/options", {"limit": 2}).json()["truncated"] is True


def test_options_skip_inactive_and_fall_back_to_id(data, approver):
    apply(approver, [{"op": "deactivate_rows", "table": "customer", "where": {"customerCode": "C005"}}])
    assert "Delta" not in [o["label"] for o in api().get("/api/schema/tables/customer/options").json()["options"]]
    r = api().get("/api/schema/tables/orderHeader/options").json()
    assert r["labelColumn"] is None


def test_label_prefers_name_then_code():
    cols = lambda *names: {n: {"type": "text"} for n in names}
    assert rows.label_column(cols("customerCode", "productName")) == "productName"
    assert rows.label_column(cols("note", "customerCode")) == "customerCode"
    assert rows.label_column(cols("note", "other")) == "note"
    assert rows.label_column({"id": {"type": "bigint"}, "qty": {"type": "integer"}}) is None


# ---- a grid save goes through the gate like everything else ---------------------------

def test_grid_edits_become_one_proposal_and_show_up(data):
    editor = api(Role.EDITOR)
    ids = [x["id"] for x in get(editor, "customer").json()["rows"]]
    ops = [
        {"op": "update_rows", "table": "customer", "where": {"id": ids[:2]}, "set": {"creditLimit": 999}},
        {"op": "update_rows", "table": "customer", "where": {"id": ids[2]}, "set": {"name": "Renamed"}},
        {"op": "insert_rows", "table": "customer", "rows": [{"customerCode": "C100", "name": "New", "creditLimit": 5}]},
    ]
    ch = editor.post("/api/changes", json.dumps({"operations": ops, "source": "grid", "prompt": "grid edit"}),
                     content_type="application/json").json()
    assert (ch["status"], ch["kind"], ch["source"]) == ("pending", "data", "grid")
    assert editor.post(f"/api/changes/{ch['id']}/approve", "{}", content_type="application/json").json()["status"] == "applied"
    after = get(editor, "customer", limit=50).json()
    assert after["total"] == 6
    by = {x["customerCode"]: x for x in after["rows"]}
    assert by["C001"]["creditLimit"] == "999.00" and by["C003"]["name"] == "Renamed"
    assert by["C001"]["updatedAt"] and by["C100"]["createdBy"] == "editor@z.com"


def test_viewer_cannot_save_from_the_grid(data):
    r = api().post("/api/changes", json.dumps({"operations": [
        {"op": "update_rows", "table": "customer", "where": {"id": 1}, "set": {"name": "x"}}], "source": "grid"}),
        content_type="application/json")
    assert r.status_code == 403
