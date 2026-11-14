import pytest

from iquery import context
from search import indexer
from search import service as search_service
from search.embedder import SearchError

from .conftest import fake_vector  # noqa: F401  (the `embed` fixture comes from conftest)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


def build(*statements):
    from schema import conn
    with conn.rw_connection(autocommit=True) as c:
        for st in statements:
            c.execute(st)


@pytest.fixture
def shop(embed):
    build(
        "CREATE TABLE master.partner (partner_id bigserial PRIMARY KEY, name text)",
        "COMMENT ON TABLE master.partner IS 'company we sign contracts with'",
        "CREATE TABLE master.contract (contract_id bigserial PRIMARY KEY, "
        "partner_id bigint REFERENCES master.partner(partner_id), status text)",
        "COMMENT ON TABLE master.contract IS 'signed contract'",
        "CREATE TABLE master.unrelated (unrelated_id bigserial PRIMARY KEY, stripe text)",
        "COMMENT ON TABLE master.unrelated IS 'zebra'",
        "INSERT INTO master.partner (name) VALUES ('บริษัท สยามพลังงาน จำกัด'), ('Acme Ltd')",
    )
    indexer.reindex(rebuild=True)


def test_query_texts_put_newest_first():
    assert context.query_texts(["a", "b"]) == ["b", "a\nb"]
    assert context.query_texts(["a"]) == ["a"]


def test_memory_text():
    assert context.memory_text(["a", "b"]) == "a\nb"
    assert context.memory_text(["only"]) == "only"


def test_pick_tables_adds_fk_neighbours(shop):
    r = context.pick_tables("master", ["signed contract"], 8)
    assert r["tables"][0] == "contract" and "partner" in r["tables"]
    assert r["searchOffline"] is False


def test_pick_tables_respects_limit(shop):
    assert len(context.pick_tables("master", ["signed contract"], 1)["tables"]) == 1


def test_pick_tables_search_offline(shop, monkeypatch):
    def boom(*a, **k):
        raise SearchError("down")
    monkeypatch.setattr(search_service, "search", boom)
    r = context.pick_tables("master", ["contract"], 8)
    assert r["searchOffline"] is True and r["tables"][0] == "contract"


def test_find_values_finds_name_in_thai_comment(shop):
    got = context.find_values("master", ["partner"], ["อยากได้ข้อมูลสัญญาของ สยามพลังงาน"], 800)
    assert {"table": "partner", "column": "name", "value": "บริษัท สยามพลังงาน จำกัด"} in got


def test_value_lookup_timeout_is_not_fatal(shop):
    build("INSERT INTO master.partner (name) SELECT md5(g::text) FROM generate_series(1, 200000) g")
    assert context.find_values("master", ["partner"], ["acme"], 1) == []


def test_describe_quotes_mixed_case(shop):
    build('CREATE TABLE master."sppProject" (id integer PRIMARY KEY, "projectName" text)')
    text = context.describe_for_prompt("master", ["sppProject", "contract"])
    assert '"sppProject"' in text and '"projectName"' in text
    assert 'FK "partner_id" -> "partner"."partner_id"' in text
