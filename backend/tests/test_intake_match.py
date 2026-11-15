import pytest
from django.contrib.auth import get_user_model

from approvals.models import Change
from intake import match as matcher
from intake.models import ImportRecipe
from intake.profile import profile
from search import embedder, indexer
from search.embedder import SearchError

from .conftest import (
    fake_vector,  # noqa: F401  (the `embed` fixture comes from conftest)
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


def build(*statements):
    from schema import conn
    with conn.rw_connection(autocommit=True) as c:
        for st in statements:
            c.execute(st)


@pytest.fixture
def shop(embed):
    build(
        "CREATE TABLE master.province (province_id bigserial PRIMARY KEY, name text UNIQUE)",
        "CREATE TABLE master.partner (partner_id bigserial PRIMARY KEY, name text, tax_id text UNIQUE, "
        "province_id bigint REFERENCES master.province(province_id))",
        "COMMENT ON TABLE master.partner IS 'company we sign contracts with'",
        "CREATE TABLE master.unrelated (unrelated_id bigserial PRIMARY KEY, stripe text)",
        "INSERT INTO master.province (name) VALUES ('Bangkok'), ('Chiang Mai'), ('Phuket')",
        "INSERT INTO master.partner (name, tax_id) VALUES ('Acme Company Limited', '0105500000001'), "
        "('Siam Steel', '0105500000002')",
    )
    indexer.reindex(rebuild=True)


def source(name, headers, rows):
    return {"name": name, "headers": headers, "rows": rows, "notes": [], "truncated": False}


VENDORS = source("Vendors", ["Vendor Name", "Tax ID", "Province"], [
    ["Acme Co., Ltd.", "0105500000099", "Bangkok"],
    ["Siam Steel Trading", "0105500000002", "Chiang Mai"],
    ["Brand New Shop", "0105500000123", "Phuket"],
])


def run(src=VENDORS, row_match=True, schema="master"):
    return matcher.match(schema, [src], [profile(src, 5)], row_match)


# --- fingerprint and recipe ---

def test_fingerprint_ignores_order_case_and_spaces():
    a = matcher.fingerprint(["Vendor Name", "Tax ID"])
    assert a == matcher.fingerprint(["  tax id ", "VENDOR   NAME"])
    assert a != matcher.fingerprint(["Vendor Name", "Tax"])
    assert len(a) == 64


@pytest.fixture
def applied_change():
    user = get_user_model().objects.create_user(username="imp", password="x")
    return Change.objects.create(requester=user, source="web", kind="data", status=Change.Status.APPLIED)


def test_approved_recipe_for_same_headers_is_returned(shop, applied_change):
    r = ImportRecipe.objects.create(schema="master", fingerprint=matcher.fingerprint(VENDORS["headers"]),
                                    headers=VENDORS["headers"], mapping={"m": 1}, filters=[{"f": 1}],
                                    approved_change=applied_change)
    got = run()
    assert got["recipe"] == {"id": r.pk, "mapping": {"m": 1}, "filters": [{"f": 1}]}


def test_recipe_for_another_schema_is_not_returned(shop, applied_change):
    ImportRecipe.objects.create(schema="crm", fingerprint=matcher.fingerprint(VENDORS["headers"]),
                                mapping={"m": 1}, approved_change=applied_change)
    assert run()["recipe"] is None


def test_recipe_without_applied_change_is_not_returned(shop):
    ImportRecipe.objects.create(schema="master", fingerprint=matcher.fingerprint(VENDORS["headers"]), mapping={})
    assert run()["recipe"] is None


# --- columns and tables ---

def test_vendor_name_ranks_partner_name_first(shop, embed):
    got = run()
    assert got["searchOffline"] is False
    cands = got["sources"]["Vendors"]["columns"]["Vendor Name"]
    assert cands[0][:2] == ["partner", "name"]
    assert len(cands) <= 3 and all(isinstance(c[2], float) for c in cands)
    assert cands == sorted(cands, key=lambda c: -c[2])
    # the column text is "<header> (<type>): <samples>" embedded as a query
    assert embed[-1][0] == "query"
    assert "Vendor Name (text): Acme Co., Ltd., Siam Steel Trading, Brand New Shop" in embed[-1][1]
    tables = got["sources"]["Vendors"]["tables"]
    assert tables[0][0] == "partner" and len(tables) <= 3


def test_table_score_is_sum_of_best_column_scores(shop):
    s = run()["sources"]["Vendors"]
    want = {}
    for cands in s["columns"].values():
        if cands:
            want[cands[0][0]] = want.get(cands[0][0], 0) + cands[0][2]
    assert {t: round(v, 4) for t, v in s["tables"]} == {t: round(v, 4) for t, v in
                                                         sorted(want.items(), key=lambda kv: -kv[1])[:3]}


def test_search_offline_uses_name_similarity(shop, monkeypatch):
    def boom(*a, **k):
        raise SearchError("down")
    monkeypatch.setattr(embedder, "embed", boom)
    got = run()
    assert got["searchOffline"] is True
    assert got["sources"]["Vendors"]["columns"]["Tax ID"][0][:2] == ["partner", "tax_id"]
    assert got["sources"]["Vendors"]["tables"][0][0] == "partner"


# --- refs ---

def test_province_column_is_a_ref_to_province(shop):
    refs = run()["sources"]["Vendors"]["refs"]
    assert refs["Province"] == {"table": "province", "key": "name"}
    assert "Vendor Name" not in refs and "Tax ID" not in refs


# --- rows ---

def test_exact_business_key_exists(shop):
    rows = run()["sources"]["Vendors"]["rows"]
    assert rows["table"] == "partner"
    siam = next(i for i, r in enumerate(VENDORS["rows"]) if r[1] == "0105500000002") + 1
    from schema import conn
    with conn.ro_connection() as c:
        sid = c.execute("SELECT partner_id FROM master.partner WHERE tax_id = '0105500000002'").fetchone()["partner_id"]
    assert rows["exists"] == [{"row": siam, "id": sid}]
    assert all(m["row"] != siam for m in rows["maybe"])


def test_name_match_is_maybe_not_exists(shop):
    rows = run()["sources"]["Vendors"]["rows"]
    from schema import conn
    with conn.ro_connection() as c:
        aid = c.execute("SELECT partner_id FROM master.partner WHERE name = 'Acme Company Limited'").fetchone()["partner_id"]
    assert [m for m in rows["maybe"] if m["row"] == 1] == [
        {"row": 1, "id": aid, "score": rows["maybe"][0]["score"], "existing": "Acme Company Limited"}]
    assert isinstance(rows["maybe"][0]["score"], float)
    assert all(e["row"] != 1 for e in rows["exists"])
    assert all(m["row"] != 3 for m in rows["maybe"])  # "Brand New Shop" is new
    assert 1 <= len(rows["examples"]) <= 20


def test_examples_are_capped(shop):
    many = source("Vendors", ["Vendor Name", "Tax ID", "Province"],
                  [["Siam Steel", "0105500000002", "Bangkok"]] * 30)
    rows = run(many)["sources"]["Vendors"]["rows"]
    assert len(rows["exists"]) == 30 and len(rows["examples"]) == 20


def test_row_match_false_has_no_rows(shop):
    assert "rows" not in run(row_match=False)["sources"]["Vendors"]


# --- edges ---

def test_empty_sources():
    assert matcher.match("master", [], [], True) == {"recipe": None, "searchOffline": False, "sources": {}}


def test_source_without_profile_still_matches(shop):
    got = matcher.match("master", [VENDORS], [], True)
    assert got["sources"]["Vendors"]["columns"]["Vendor Name"][0][:2] == ["partner", "name"]


def test_source_with_no_rows(shop):
    empty = source("Empty", ["Vendor Name"], [])
    got = matcher.match("master", [empty], [profile(empty, 5)], True)
    s = got["sources"]["Empty"]
    assert s["refs"] == {}
    assert s.get("rows", {"exists": [], "maybe": []})["exists"] == []


# --- maybe keeps direct incoming/existing pairs only (the engine merges pairs into clusters) ---

def _ids(*names):
    from schema import conn
    with conn.ro_connection() as c:
        return [c.execute("SELECT partner_id FROM master.partner WHERE name = %s", (n,)).fetchone()["partner_id"]
                for n in names]


def test_incoming_chain_does_not_reach_existing(shop):
    # existing X ~ incoming A (row 1), A ~ incoming B (row 2), but B is not like X
    build("INSERT INTO master.partner (name, tax_id) VALUES ('Kanchana Rubber', '0105500000301')")
    src = source("Vendors", ["Vendor Name", "Tax ID"], [
        ["Kanchana Rubber Plantation", "0105500000401"],
        ["Kanchana Plantation", "0105500000402"],
    ])
    maybe = run(src)["sources"]["Vendors"]["rows"]["maybe"]
    (x,) = _ids("Kanchana Rubber")
    assert [(m["row"], m["id"], m["existing"]) for m in maybe] == [(1, x, "Kanchana Rubber")]


def test_incoming_pairs_with_the_existing_row_it_matches(shop):
    # existing X ~ existing Y (X has the smaller id); incoming A matches only Y
    build("INSERT INTO master.partner (name, tax_id) VALUES ('Kanchana Plantation', '0105500000301')",
          "INSERT INTO master.partner (name, tax_id) VALUES ('Kanchana Rubber Plantation', '0105500000302')")
    src = source("Vendors", ["Vendor Name", "Tax ID"], [["Kanchana Rubber", "0105500000401"]])
    maybe = run(src)["sources"]["Vendors"]["rows"]["maybe"]
    x, y = _ids("Kanchana Plantation", "Kanchana Rubber Plantation")
    assert x < y
    assert [(m["row"], m["id"], m["existing"]) for m in maybe] == [(1, y, "Kanchana Rubber Plantation")]
    import singularity_engine
    want = singularity_engine.find_duplicates([(y, ["Kanchana Rubber Plantation"]), (-1, ["Kanchana Rubber"])], 0.9)
    assert maybe[0]["score"] == round(want[0][1], 4)
