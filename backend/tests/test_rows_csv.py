import csv
import io
import json
import re

import pytest
from django.http import StreamingHttpResponse
from django.test import Client

from accounts.models import Role, User
from approvals import service
from audit.models import AuditLog
from schema import rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


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
            {"name": "customer_code", "type": "text", "unique": True}, {"name": "name", "type": "text"},
            {"name": "credit_limit", "type": "numeric(12,2)", "nullable": True}]},
        {"op": "create_table", "name": "order_header", "columns": [
            {"name": "customer_id", "type": "bigint", "references": {"table": "customer"}}]},
    ])
    names = ["Acme", "Beta 100%", "Gamma_Co", "สมชาย การค้า", "Delta"]
    apply(approver, [{"op": "insert_rows", "table": "customer", "rows": [
        {"customer_code": f"C{i:03d}", "name": n, "credit_limit": i * 10} for i, n in enumerate(names, 1)]}])


def api(role=Role.EDITOR):
    u, _ = User.objects.get_or_create(username=f"{role}@z.com", defaults={"email": f"{role}@z.com", "role": role})
    c = Client()
    c.force_login(u)
    return c


def export(c, table="customer", **params):
    if "filters" in params:
        params["filters"] = json.dumps(params["filters"])
    return c.get(f"/api/schema/tables/{table}/export.csv", params)


def body(resp):
    raw = b"".join(resp.streaming_content) if isinstance(resp, StreamingHttpResponse) else resp.content
    return raw.decode("utf-8")


def parsed(resp):
    text = body(resp)
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:], newline="")))


def test_csv_has_bom_header_and_crlf(data):
    r = export(api())
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert r["Cache-Control"] == "no-store"
    assert re.fullmatch(r'attachment; filename="master-customer-\d{4}-\d{2}-\d{2}\.csv"', r["Content-Disposition"])
    text = body(r)
    assert text.startswith("﻿")
    cols = [c["name"] for c in json.loads(api().get("/api/schema/tables/customer/rows").content)["columns"]]
    assert text[1:].split("\r\n")[0] == ",".join(cols)
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    assert len(text[1:].split("\r\n")) - 1 == 6  # header + 5 rows


def test_csv_quotes_commas_quotes_newlines(data, approver):
    weird = 'a,"b"\nc'
    apply(approver, [{"op": "update_rows", "table": "customer", "where": {"customer_code": "C001"},
                      "set": {"name": weird}}])
    rows_ = parsed(export(api(), filters={"customer_code": "C001"}))
    header = rows_[0]
    assert rows_[1][header.index("name")] == weird


def test_csv_thai_round_trips(data):
    assert "สมชาย การค้า" in body(export(api()))


def test_csv_null_is_empty_field(data, approver):
    apply(approver, [{"op": "update_rows", "table": "customer", "where": {"customer_code": "C002"},
                      "set": {"credit_limit": None}}])
    r = parsed(export(api(), filters={"customer_code": "C002"}))
    assert r[1][r[0].index("credit_limit")] == ""


def test_csv_matches_rows_endpoint_for_filter_sort_inactive(data, approver):
    c = api()
    params = {"filters": {"name": "a"}, "sort": "name", "dir": "desc"}
    expect = [x["customer_code"] for x in json.loads(
        c.get("/api/schema/tables/customer/rows", {**params, "filters": json.dumps(params["filters"]),
                                                    "limit": 200}).content)["rows"]]
    got = parsed(export(c, **params))
    assert [row[got[0].index("customer_code")] for row in got[1:]] == expect and expect
    apply(approver, [{"op": "deactivate_rows", "table": "customer", "where": {"customer_code": "C001"}}])
    assert len(parsed(export(c))) - 1 == 4
    assert len(parsed(export(c, inactive="1"))) - 1 == 5


def test_csv_filter_percent_underscore_literal(data):
    def names(f):
        r = parsed(export(api(), filters=f))
        return [row[r[0].index("name")] for row in r[1:]]

    assert names({"name": "%"}) == ["Beta 100%"]
    assert names({"name": "_"}) == ["Gamma_Co"]


def test_csv_over_cap_is_400_with_message(data, monkeypatch):
    from schema import views
    monkeypatch.setattr(views, "get_config", lambda: {"limits": {"maxExportRows": 3}})
    r = export(api())
    assert r.status_code == 400 and not isinstance(r, StreamingHttpResponse)
    assert json.loads(r.content)["error"] == (
        "5 rows match; the most that can be exported is 3 (setting limits.maxExportRows). Narrow the filters.")
    assert export(api(), filters={"customer_code": "C001"}).status_code == 200


def test_csv_formula_prefix_text_not_numbers(data, approver):
    evil = ["=1+1", "+1", "-x", "@s", "\tx", "\rx"]
    apply(approver, [{"op": "insert_rows", "table": "customer", "rows": [
        {"customer_code": f"E{i}", "name": n, "credit_limit": -5} for i, n in enumerate(evil)]}])
    r = parsed(export(api(), filters={"customer_code": "E"}))
    h = r[0]
    by = {row[h.index("customer_code")]: row for row in r[1:]}
    for i, n in enumerate(evil):
        assert by[f"E{i}"][h.index("name")] == "'" + n
        assert not by[f"E{i}"][h.index("credit_limit")].startswith("'")
        assert by[f"E{i}"][h.index("credit_limit")].startswith("-5")


def test_csv_reference_column_exports_id(data, approver):
    apply(approver, [{"op": "insert_rows", "table": "order_header", "rows": [{"customer_id": 2}]}])
    r = parsed(export(api(), table="order_header"))
    assert r[1][r[0].index("customer_id")] == "2"


def test_csv_viewer_403_editor_ok_anonymous_blocked(data):
    assert export(api(Role.VIEWER)).status_code == 403
    assert export(api(Role.EDITOR)).status_code == 200
    assert Client().get("/api/schema/tables/customer/export.csv").status_code in (401, 403)


def test_csv_unknown_table_and_column(data):
    c = api()
    assert export(c, table="nope").status_code == 404
    assert export(c, sort="nope").status_code == 400
    assert export(c, filters={"nope": "x"}).status_code == 400
    assert export(c, dir="up").status_code == 400
    assert export(c, filters="x").status_code == 400


def test_csv_writes_one_audit_entry(data):
    export_resp = export(api(), filters={"name": "a"})
    body(export_resp)
    es = AuditLog.objects.filter(action="table.export_csv")
    assert es.count() == 1
    d = es[0].details
    assert d["table"] == "customer" and d["schema"] == "master" and d["rows"] == 4 and d["filters"] == {"name": "a"}


def test_csv_cell_rules():
    assert rows.csv_cell(None, "text") == ""
    assert rows.csv_cell("=x", "text") == "'=x"
    assert rows.csv_cell("=x", "character varying(20)") == "'=x"
    assert rows.csv_cell(-5, "integer") == "-5"
    assert rows.csv_cell("-5", "numeric(12,2)") == "-5"
    assert rows.csv_cell(True, "boolean") == "True"
