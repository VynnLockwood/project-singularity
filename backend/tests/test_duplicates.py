import json

import pytest
from django.test import Client

from accounts.models import Role, User
from approvals import service
from duplicates import service as dup
from duplicates.models import DuplicateGroup

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

NAMES = ["Acme Trading Co., Ltd.", "ACME TRADING COMPANY LIMITED", "Zenith Foods", "บริษัท สยามพัฒนา จำกัด",
         "สยามพัฒนา", "Orion Steel", "Acme Tradng Co Ltd"]


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def apply(user, ops):
    ch = service.propose(user, ops, source="web")
    assert ch.status == "pending", ch.check_result
    return service.approve(user, ch.pk)


@pytest.fixture
def data(approver):
    apply(approver, [
        {"op": "create_table", "name": "customer", "columns": [
            {"name": "customer_code", "type": "text"}, {"name": "name", "type": "text"}]},
        {"op": "create_table", "name": "order_header", "columns": [
            {"name": "customer_id", "type": "bigint", "references": {"table": "customer"}}]},
    ])
    apply(approver, [{"op": "insert_rows", "table": "customer",
                      "rows": [{"customer_code": f"C{i}", "name": n} for i, n in enumerate(NAMES, 1)]}])
    apply(approver, [{"op": "insert_rows", "table": "order_header", "rows": [{"customer_id": 2}, {"customer_id": 7}, {"customer_id": 3}]}])


def api(role=Role.EDITOR):
    u, _ = User.objects.get_or_create(username=f"{role}@z.com", defaults={"email": f"{role}@z.com", "role": role})
    c = Client()
    c.force_login(u)
    return c


def post(c, url, body=None):
    return c.post(url, json.dumps(body or {}), content_type="application/json")


def scan(c=None, **body):
    return post(c or api(), "/api/duplicates/scan", {"table": "customer", **body})


def member_sets():
    return sorted(sorted(g.member_ids) for g in DuplicateGroup.objects.all())


# ---- scanning -----------------------------------------------------------------

def test_scan_finds_english_thai_and_typo_groups(data):
    r = scan().json()
    assert r["scanned"] == 7 and r["groups"] == 2 and r["new"] == 2 and r["fields"] == ["name"]
    assert member_sets() == [[1, 2, 7], [4, 5]]


def test_rescan_keeps_group_identity_and_dismissals(data):
    scan()
    first = {g.key: g.pk for g in DuplicateGroup.objects.all()}
    thai = DuplicateGroup.objects.get(member_ids=[4, 5])
    post(api(), f"/api/duplicates/{thai.pk}/dismiss")
    r = scan().json()
    assert r["new"] == 0 and r["removed"] == 0
    assert {g.key: g.pk for g in DuplicateGroup.objects.all()} == first
    assert DuplicateGroup.objects.get(pk=thai.pk).status == "dismissed"
    post(api(), f"/api/duplicates/{thai.pk}/reopen")
    assert DuplicateGroup.objects.get(pk=thai.pk).status == "open"


def test_groups_that_no_longer_exist_are_removed(data, approver):
    scan()
    apply(approver, [{"op": "deactivate_rows", "table": "customer", "where": {"customer_id": [4]}}])
    r = scan().json()
    assert r["removed"] == 1 and member_sets() == [[1, 2, 7]]


def test_inactive_rows_are_not_compared(data, approver):
    apply(approver, [{"op": "deactivate_rows", "table": "customer", "where": {"customer_id": [2, 7]}}])
    assert scan().json()["groups"] == 1  # only the Thai pair is left


def test_threshold_and_fields_are_validated_and_used(data):
    assert scan(threshold=0.99).json()["groups"] == 2    # only exact matches after normalising: the typo record drops out
    assert member_sets() == [[1, 2], [4, 5]]
    for bad in ({"threshold": 0.2}, {"threshold": "high"}, {"fields": ["nope"]}, {"fields": "name"}, {"fields": ["id"]}):
        assert scan(**bad).status_code == 400, bad
    assert post(api(), "/api/duplicates/scan", {"table": "ghost"}).status_code == 404
    assert scan(fields=["customer_code"]).json()["groups"] == 0   # codes C1..C7 are not duplicates


def test_row_cap_is_reported(data, monkeypatch):
    real = dup.get_config()
    monkeypatch.setattr(dup, "get_config", lambda: {**real, "duplicates": {**real["duplicates"], "maxRows": 3}})
    r = scan().json()
    assert r["truncated"] is True and r["scanned"] == 3


def test_default_fields_prefer_name_columns():
    cols = lambda *n: {x: {"type": "text"} for x in n}
    assert dup.default_fields(cols("customer_code", "tradeName", "legalName")) == ["tradeName", "legalName"]
    assert dup.default_fields(cols("note", "customer_code")) == ["customer_code"]
    assert dup.default_fields({"qty": {"type": "integer"}}) == []


# ---- listing ------------------------------------------------------------------

def test_list_filter_and_counts(data):
    scan()
    r = api(Role.VIEWER).get("/api/duplicates").json()
    assert len(r["groups"]) == 2 and r["tables"] == {"customer": 2}
    g = DuplicateGroup.objects.get(member_ids=[4, 5])
    post(api(), f"/api/duplicates/{g.pk}/dismiss")
    assert len(api().get("/api/duplicates", {"status": "open"}).json()["groups"]) == 1
    assert api().get("/api/duplicates", {"status": "dismissed"}).json()["groups"][0]["id"] == g.pk
    assert api().get("/api/duplicates", {"table": "ghost"}).json()["groups"] == []


def test_detail_shows_member_records_and_who_points_at_them(data):
    scan()
    g = DuplicateGroup.objects.get(member_ids=[1, 2, 7])
    d = api(Role.VIEWER).get(f"/api/duplicates/{g.pk}").json()
    assert [m["customer_id"] for m in d["members"]] == [1, 2, 7] and d["members"][0]["name"] == NAMES[0]
    assert d["referencedBy"] == [{"table": "order_header", "column": "customer_id"}]
    assert api().get("/api/duplicates/99999").status_code == 404


def test_detail_hides_audit_columns_but_keeps_the_primary_key(approver):
    apply(approver, [{"op": "create_table", "name": "vendor", "columns": [
        {"name": "name", "type": "text"}, {"name": "created_at", "type": "timestamptz"},
        {"name": "updated_at", "type": "timestamptz"}, {"name": "created_by", "type": "text"},
        {"name": "is_active", "type": "boolean"}]}])
    apply(approver, [{"op": "insert_rows", "table": "vendor", "rows": [{"name": "Acme"}, {"name": "Acme"}]}])
    g = DuplicateGroup.objects.create(table_name="vendor", fields=["name"], key="k", member_ids=[1, 2], score=1.0)
    d = api(Role.VIEWER).get(f"/api/duplicates/{g.pk}").json()
    for m in d["members"]:
        assert set(m) == {"vendor_id", "name"}
    assert d["primaryKey"] == "vendor_id"
    assert d["hiddenColumns"] == ["created_at", "created_by", "is_active", "updated_at"]


def test_detail_keeps_every_data_column(data):
    g = DuplicateGroup.objects.create(table_name="customer", fields=["name"], key="k2", member_ids=[1, 2, 7], score=1.0)
    d = api(Role.VIEWER).get(f"/api/duplicates/{g.pk}").json()
    assert set(d["members"][0]) == {"customer_id", "customer_code", "name"}
    assert set(d["hiddenColumns"]) == {"created_at", "updated_at", "created_by", "is_active"}


# ---- permissions ----------------------------------------------------------------

def test_viewers_can_look_but_not_change_anything(data):
    scan()
    g = DuplicateGroup.objects.first()
    v = api(Role.VIEWER)
    assert v.get("/api/duplicates").status_code == 200
    assert scan(v).status_code == 403
    assert post(v, f"/api/duplicates/{g.pk}/dismiss").status_code == 403
    assert post(v, f"/api/duplicates/{g.pk}/reopen").status_code == 403
    assert post(v, f"/api/duplicates/{g.pk}/merge", {"survivorId": 1}).status_code == 403
    assert Client().get("/api/duplicates").status_code == 401


# ---- merging --------------------------------------------------------------------

def test_merge_operations_repoint_references_then_deactivate(data):
    scan()
    g = DuplicateGroup.objects.get(member_ids=[1, 2, 7])
    assert dup.merge_operations(g, 1) == [
        {"op": "update_rows", "table": "order_header", "where": {"customer_id": [2, 7]}, "set": {"customer_id": 1}},
        {"op": "deactivate_rows", "table": "customer", "where": {"customer_id": [2, 7]}},
    ]
    g2 = DuplicateGroup.objects.get(member_ids=[4, 5])
    assert dup.merge_operations(g2, 5)[-1] == {"op": "deactivate_rows", "table": "customer", "where": {"customer_id": 4}}
    with pytest.raises(dup.DuplicateError, match="one of the group"):
        dup.merge_operations(g, 3)


def test_merge_goes_through_the_gate_and_cleans_up(data):
    scan()
    g = DuplicateGroup.objects.get(member_ids=[1, 2, 7])
    ed = api()
    r = post(ed, f"/api/duplicates/{g.pk}/merge", {"survivorId": 1})
    assert r.status_code == 201
    change = r.json()["change"]
    assert (change["status"], change["kind"], change["source"]) == ("pending", "data", "web")
    assert r.json()["group"]["status"] == "merge_pending"
    assert post(ed, f"/api/duplicates/{g.pk}/merge", {"survivorId": 1}).status_code == 409   # one merge at a time

    assert post(ed, f"/api/changes/{change['id']}/approve").json()["status"] == "applied"   # editor self-approves data
    assert api().get("/api/duplicates", {"table": "customer"}).json()["groups"][0]["status"] in ("merged", "open", "dismissed")
    assert dup.state(DuplicateGroup.objects.get(pk=g.pk)) == "merged"

    from schema.query import run_select
    orders = run_select('SELECT "customer_id" FROM \"order_header\" ORDER BY customer_id')["rows"]
    assert orders == [[1], [1], [3]]                       # 2 and 7 now point at the survivor
    active = run_select('SELECT customer_id FROM customer WHERE is_active ORDER BY customer_id')["rows"]
    assert [x[0] for x in active] == [1, 3, 4, 5, 6]        # losers are deactivated, not deleted
    assert run_select("SELECT count(*) FROM customer")["rows"] == [[7]]

    assert scan().json()["groups"] == 1                    # the merged group is gone on the next scan
    assert member_sets() == [[4, 5]]


def test_a_rejected_merge_leaves_the_group_open(data, approver):
    scan()
    g = DuplicateGroup.objects.get(member_ids=[1, 2, 7])
    ch = service.propose(approver, dup.merge_operations(g, 1), source="web")
    r = post(api(), f"/api/duplicates/{g.pk}/merge", {"survivorId": 1}).json()
    service.reject(approver, r["change"]["id"], "no")
    assert dup.state(DuplicateGroup.objects.get(pk=g.pk)) == "open"
    assert post(api(), f"/api/duplicates/{g.pk}/merge", {"survivorId": 2}).status_code == 201   # can try again
    assert ch.pk != r["change"]["id"]


@pytest.mark.parametrize("body", [{}, {"survivorId": "1"}, {"survivorId": True}, {"survivorId": 5}, {"survivorId": 999}])
def test_merge_needs_a_valid_survivor(data, body):
    scan()
    g = DuplicateGroup.objects.get(member_ids=[1, 2, 7])
    assert post(api(), f"/api/duplicates/{g.pk}/merge", body).status_code == 400


def test_scan_and_dismiss_are_audited(data):
    scan()
    g = DuplicateGroup.objects.first()
    post(api(), f"/api/duplicates/{g.pk}/dismiss")
    from audit.models import AuditLog
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"duplicates.scanned", "duplicates.dismissed"} <= actions
