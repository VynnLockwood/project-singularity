import hashlib
import json
import math
import re

import pytest
from django.test import Client

from accounts.models import Role, User
from approvals import service
from search import embedder, indexer
from search import service as search_service
from search.embedder import SearchError
from search.models import DIMENSIONS, SchemaEmbedding

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


def fake_vector(text: str) -> list[float]:
    """Deterministic stand-in for the model: hashed bag of words, unit length. Shared words = similar."""
    v = [0.0] * DIMENSIONS
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIMENSIONS] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture
def embed(settings, monkeypatch):
    settings.EMBED_URL = "http://embed.test"
    calls = []

    def fake(texts, kind):
        calls.append((kind, list(texts)))
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(embedder, "embed", fake)
    return calls


@pytest.fixture
def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def apply(user, ops):
    ch = service.propose(user, ops, source="web")
    assert ch.status == "pending", ch.check_result
    return service.approve(user, ch.pk)


@pytest.fixture
def schema(approver):
    apply(approver, [
        {"op": "create_table", "name": "customer", "columns": [
            {"name": "customerCode", "type": "text", "unique": True, "comment": "unique code of the customer"},
            {"name": "phoneNumber", "type": "text", "comment": "main contact phone"}]},
        {"op": "create_table", "name": "product", "comment": "things we sell", "columns": [
            {"name": "productName", "type": "text", "comment": "display name of the product"}]},
    ])
    apply(approver, [{"op": "insert_rows", "table": "customer",
                      "rows": [{"customerCode": "C001", "phoneNumber": "081-234-5678"}]}])


def client(role):
    u, _ = User.objects.get_or_create(username=f"{role}@z.com", defaults={"email": f"{role}@z.com", "role": role})
    c = Client()
    c.force_login(u)
    return c


# ---- indexing -----------------------------------------------------------------

def test_indexes_tables_and_columns_but_not_audit_columns(embed, schema):
    r = indexer.reindex()
    keys = set(SchemaEmbedding.objects.values_list("kind", "table_name", "column_name"))
    assert keys == {("table", "customer", ""), ("table", "product", ""), ("column", "customer", "customerCode"),
                    ("column", "customer", "phoneNumber"), ("column", "product", "productName")}
    assert r == {"embedded": 5, "unchanged": 0, "removed": 0}
    assert all(kind == "passage" for kind, _ in embed)


def test_documents_carry_comment_type_and_sample_values(embed, schema):
    indexer.reindex()
    doc = SchemaEmbedding.objects.get(table_name="customer", column_name="phoneNumber").content
    assert "main contact phone" in doc and "(text)" in doc and "081-234-5678" in doc


def test_reindex_only_embeds_what_changed(embed, schema, approver):
    indexer.reindex()
    embed.clear()
    assert indexer.reindex() == {"embedded": 0, "unchanged": 5, "removed": 0}
    assert embed == []
    apply(approver, [{"op": "set_column_comment", "table": "product", "column": "productName", "comment": "shown on receipts"}])
    r = indexer.reindex()
    assert r["embedded"] == 1 and r["unchanged"] == 4
    assert indexer.reindex(rebuild=True)["embedded"] == 5


def test_dropped_things_leave_the_index(embed, schema, approver):
    indexer.reindex()
    apply(approver, [{"op": "drop_column", "table": "customer", "column": "phoneNumber", "reason": "unused"}])
    assert indexer.reindex()["removed"] == 1
    assert not SchemaEmbedding.objects.filter(column_name="phoneNumber").exists()
    apply(approver, [{"op": "drop_table", "table": "product", "reason": "unused"}])
    assert indexer.reindex()["removed"] == 2
    assert set(SchemaEmbedding.objects.values_list("table_name", flat=True)) == {"customer"}


def test_foreign_keys_are_described(embed, approver):
    apply(approver, [
        {"op": "create_table", "name": "customer", "columns": [{"name": "name", "type": "text"}]},
        {"op": "create_table", "name": "orderHeader", "columns": [
            {"name": "customerId", "type": "bigint", "references": {"table": "customer"}}]}])
    indexer.reindex()
    doc = SchemaEmbedding.objects.get(table_name="orderHeader", column_name="customerId").content
    assert "References customer.id" in doc


def test_wrong_vector_size_is_refused(settings, monkeypatch, schema):
    settings.EMBED_URL = "http://embed.test"
    monkeypatch.setattr(embedder, "embed", lambda texts, kind: [[0.1] * 10 for _ in texts])
    with pytest.raises(SearchError, match="dimensions"):
        indexer.reindex()
    assert SchemaEmbedding.objects.count() == 0


def test_failed_embedding_leaves_the_old_index_alone(embed, schema, monkeypatch, approver):
    indexer.reindex()
    apply(approver, [{"op": "set_column_comment", "table": "product", "column": "productName", "comment": "new"}])

    def down(texts, kind):
        raise SearchError("the embedding service is unreachable")

    monkeypatch.setattr(embedder, "embed", down)
    with pytest.raises(SearchError):
        indexer.reindex()
    assert SchemaEmbedding.objects.count() == 5


# ---- searching ----------------------------------------------------------------

def test_meaning_match_ranks_the_right_column_first(embed, schema):
    indexer.reindex()
    r = search_service.search("contact phone")
    assert (r["results"][0]["table"], r["results"][0]["column"]) == ("customer", "phoneNumber")
    r = search_service.search("name shown for a product")
    assert r["results"][0]["column"] == "productName"


def test_exact_name_outranks_a_loose_match(embed, schema):
    indexer.reindex()
    r = search_service.search("customerCode")
    assert (r["results"][0]["table"], r["results"][0]["column"]) == ("customer", "customerCode")
    assert r["confident"] is True  # a name match is a confident match
    assert search_service.search("customer")["results"][0]["column"] is None  # the table itself
    assert search_service.search("customer code")["results"][0]["column"] == "customerCode"


def test_confidence_threshold_decides_the_suggestion(embed, schema, monkeypatch):
    indexer.reindex()
    nothing = search_service.search("zebra quantum giraffe")
    assert nothing["confident"] is False and "propose a new column" in nothing["suggestion"]
    real = search_service.get_config()
    monkeypatch.setattr(search_service, "get_config", lambda: {**real, "search": {**real["search"], "minConfidence": 0.0}})
    sure = search_service.search("contact phone")
    assert sure["confident"] is True and sure["suggestion"] is None


def test_top_k_is_respected_and_capped(embed, schema):
    indexer.reindex()
    assert len(search_service.search("customer", top_k=2)["results"]) == 2
    assert len(search_service.search("customer", top_k=999)["results"]) <= 20


def test_empty_index_and_empty_query(embed):
    assert search_service.search("anything") == {
        "results": [], "confident": False, "suggestion": search_service.search("anything")["suggestion"]}
    with pytest.raises(SearchError, match="required"):
        search_service.search("   ")


# ---- REST ---------------------------------------------------------------------

def test_rest_search_any_signed_in_role(embed, schema):
    indexer.reindex()
    r = client(Role.VIEWER).get("/api/search", {"q": "contact phone"})
    assert r.status_code == 200 and r.json()["results"][0]["column"] == "phoneNumber"
    assert Client().get("/api/search", {"q": "x"}).status_code == 401
    assert client(Role.VIEWER).get("/api/search", {"q": ""}).status_code == 400
    assert client(Role.VIEWER).get("/api/search", {"q": "x", "k": "abc"}).status_code == 400


def test_rest_search_when_embedding_is_down_is_503(settings, monkeypatch):
    settings.EMBED_URL = ""
    r = client(Role.VIEWER).get("/api/search", {"q": "x"})
    assert r.status_code == 503 and "not configured" in r.json()["error"]


def test_reindex_endpoint_is_approver_only_and_audited(embed, schema):
    body = json.dumps({"rebuild": True})
    assert client(Role.EDITOR).post("/api/search/reindex", body, content_type="application/json").status_code == 403
    r = client(Role.APPROVER).post("/api/search/reindex", body, content_type="application/json")
    assert r.status_code == 200 and r.json()["embedded"] == 5
    from audit.models import AuditLog
    assert AuditLog.objects.filter(action="search.reindexed").exists()


# ---- automatic reindexing -------------------------------------------------------

def test_applied_schema_change_schedules_a_reindex(embed, approver, monkeypatch, django_capture_on_commit_callbacks):
    hits = []
    monkeypatch.setattr(indexer, "schedule_reindex", lambda: hits.append(1))
    with django_capture_on_commit_callbacks(execute=True):
        apply(approver, [{"op": "create_table", "name": "supplier", "columns": [{"name": "name", "type": "text"}]}])
    assert hits == [1]
    hits.clear()
    with django_capture_on_commit_callbacks(execute=True):  # data changes do not
        apply(approver, [{"op": "insert_rows", "table": "supplier", "rows": [{"name": "A"}]}])
    assert hits == []


def test_schedule_reindex_does_nothing_without_a_service(settings, monkeypatch):
    settings.EMBED_URL = ""
    monkeypatch.setattr(indexer.threading, "Thread", lambda *a, **k: pytest.fail("started a thread"))
    indexer.schedule_reindex()


# ---- the embed client ------------------------------------------------------------

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


def test_client_batches_and_sends_kind(settings, monkeypatch):
    settings.EMBED_URL = "http://e"
    sent = []

    def post(url, json, timeout):
        sent.append((url, json["kind"], len(json["texts"])))
        return Resp(200, {"embeddings": [[0.0] * 3 for _ in json["texts"]]})

    monkeypatch.setattr(embedder.requests, "post", post)
    out = embedder.embed(["t"] * 70, "passage")
    assert len(out) == 70 and sent == [("http://e/embed", "passage", 32), ("http://e/embed", "passage", 32), ("http://e/embed", "passage", 6)]


@pytest.mark.parametrize("resp,msg", [
    (Resp(500), "HTTP 500"), (Resp(200, {"nope": 1}), "unreadable"), (Resp(200, {"embeddings": []}), "wrong number"),
])
def test_client_errors(settings, monkeypatch, resp, msg):
    settings.EMBED_URL = "http://e"
    monkeypatch.setattr(embedder.requests, "post", lambda *a, **k: resp)
    with pytest.raises(SearchError, match=msg):
        embedder.embed(["x"], "query")


def test_client_unreachable(settings, monkeypatch):
    settings.EMBED_URL = "http://e"

    def boom(*a, **k):
        raise embedder.requests.ConnectionError("x")

    monkeypatch.setattr(embedder.requests, "post", boom)
    with pytest.raises(SearchError, match="unreachable"):
        embedder.embed(["x"], "query")


# ---- through the assistant's and MCP's tool -----------------------------------------

def test_search_tool_for_viewers(embed, schema):
    from mcp_server import tools
    indexer.reindex()
    viewer = User.objects.create(username="v@x.com", email="v@x.com", role=Role.VIEWER)
    assert tools.allowed(viewer, "search_schema")
    assert tools.IMPL["search_schema"](viewer, "contact phone")["results"][0]["column"] == "phoneNumber"


def test_search_tool_reports_failures_as_tool_errors(settings):
    from mcp_server import tools
    settings.EMBED_URL = ""
    v = User.objects.create(username="v@x.com", email="v@x.com", role=Role.VIEWER)
    with pytest.raises(tools.ToolFailure, match="not configured"):
        tools.search_schema(v, "x")
