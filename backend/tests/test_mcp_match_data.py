"""match_data and get_import_recipe: the local Profiler + Matcher and saved recipes, for an AI client that designs
the mapping itself. Read-only: nothing the caller sends is stored."""
import pytest
from django.contrib.auth import get_user_model

from accounts.models import Role
from approvals.models import Change
from intake import match as matcher
from intake.models import ImportMessage, ImportRecipe, ImportRun
from mcp_server import tools
from scripts.models import Script
from search import embedder, indexer
from search.embedder import SearchError

from .conftest import (
    fake_vector,  # noqa: F401  (the `embed` fixture comes from conftest)
)
from .mcp_helpers import make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

HEADERS = ["Vendor Name", "Tax ID", "Province"]
ROWS = [
    ["Acme Co., Ltd.", "0105500000099", "Bangkok"],
    ["Siam Steel Trading", "0105500000002", "Chiang Mai"],
    ["Brand New Shop", "0105500000123", "Phuket"],
]


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


@pytest.fixture
def users():
    return make_users()


def _id(sql_text):
    from schema import conn
    with conn.ro_connection() as c:
        return next(iter(c.execute(sql_text).fetchone().values()))


def col(out, header):
    return next(c for c in out["profile"]["columns"] if c["header"] == header)


# --- roles ---

def test_both_tools_are_for_viewers(users):
    for name in ("match_data", "get_import_recipe"):
        assert tools.TOOL_ROLES[name] == "viewer" == tools.TOOL_ROLES["read_rows"]
        assert name in tools.IMPL
        assert tools.allowed(users[Role.VIEWER], name)
        assert not tools.allowed(None, name)


# --- profile ---

def test_leading_zero_codes_stay_text(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", ["Code", "Qty"],
                           [["007", 1], ["012", 2.0], ["120", 3]], row_match=False)
    code = col(out, "Code")
    assert code["type"] == "text" and code["samples"] == ["007", "012", "120"]
    assert code["distinct"] == 3 and code["empty"] == 0.0
    qty = col(out, "Qty")
    assert qty["type"] == "integer" and qty["samples"] == ["1", "2", "3"]   # 2.0 read as the number 2
    assert out["profile"]["rows"] == 3


def test_samples_are_at_most_five(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", ["Vendor Name"], [[f"v{i}"] for i in range(20)],
                           row_match=False)
    assert len(col(out, "Vendor Name")["samples"]) == 5


# --- matching ---

def test_vendor_name_ranks_partner_name_first(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS, sheet="Vendors")
    assert out["sheet"] == "Vendors" and out["searchOffline"] is False
    m = out["match"]
    assert m["columns"]["Vendor Name"][0][:2] == ["partner", "name"]
    assert all(len(c) <= 3 for c in m["columns"].values())
    assert m["tables"][0][0] == "partner" and len(m["tables"]) <= 3
    assert m["refs"]["Province"] == {"table": "province", "key": "name"}


def test_exact_business_key_exists(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS)
    rows = out["match"]["rows"]
    sid = _id("SELECT partner_id FROM master.partner WHERE tax_id = '0105500000002'")
    assert rows["table"] == "partner" and rows["key"] == "tax_id" and rows["header"] == "Tax ID"
    assert rows["exists"] == [{"row": 2, "id": sid}]            # 1-based, like the Matcher
    aid = _id("SELECT partner_id FROM master.partner WHERE name = 'Acme Company Limited'")
    assert [(m["row"], m["id"], m["existing"]) for m in rows["maybe"]] == [(1, aid, "Acme Company Limited")]


def test_search_offline_falls_back_to_names(shop, users, monkeypatch):
    def boom(*a, **k):
        raise SearchError("down")
    monkeypatch.setattr(embedder, "embed", boom)
    out = tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS)
    assert out["searchOffline"] is True
    assert out["match"]["columns"]["Tax ID"][0][:2] == ["partner", "tax_id"]


def test_row_match_false_has_no_rows(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS, row_match=False)
    assert "rows" not in out["match"]


def test_existing_values_need_the_read_rows_role(shop, users, monkeypatch):
    monkeypatch.setitem(tools.TOOL_ROLES, "read_rows", "editor")   # as if reading rows were for editors
    viewer = tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS)["match"]["rows"]
    assert viewer["maybe"] and all("existing" not in m for m in viewer["maybe"])
    assert all("existing" not in e for e in viewer["examples"])
    assert viewer["exists"]                                         # ids are still there
    editor = tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)["match"]["rows"]
    assert editor["maybe"][0]["existing"] == "Acme Company Limited"


# --- input checks ---

def test_too_many_rows_is_a_clear_error(shop, users):
    cap = tools.MATCH_MAX_ROWS
    with pytest.raises(tools.ToolFailure) as e:
        tools.match_data(users[Role.VIEWER], "master", ["a"], [["x"]] * (cap + 1))
    assert str(cap) in str(e.value) and "sample" in str(e.value) and "begin_load" in str(e.value)
    with pytest.raises(tools.ToolFailure):
        tools.match_data(users[Role.VIEWER], "master", ["a"], [["x"]] * 10, max_rows=5)


def test_bad_inputs(shop, users):
    v = users[Role.VIEWER]
    with pytest.raises(tools.ToolFailure, match="headers"):
        tools.match_data(v, "master", [], [])
    with pytest.raises(tools.ToolFailure, match="rows"):
        tools.match_data(v, "master", ["a"], "nope")
    with pytest.raises(tools.ToolFailure, match="rows"):
        tools.match_data(v, "master", ["a"], [{"a": 1}])
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.match_data(v, "nope", ["a"], [["x"]])


def test_duplicate_and_blank_headers_are_renamed_like_the_reader(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", ["Name", "Name", None, "  ", " Code "],
                           [["a", "b", "c", "d", "e"]], row_match=False)
    assert out["headers"] == ["Name", "Name_2", "column_3", "column_4", "Code"]
    assert [c["header"] for c in out["profile"]["columns"]] == out["headers"]
    assert set(out["match"]["columns"]) == set(out["headers"])
    assert any("Name_2" in n for n in out["notes"]) and any("column_3" in n for n in out["notes"])


def test_odd_cell_values_become_text(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", ["A", "B"],
                           [[True, {"x": 1}], [None, [1, 2]], ["", 3]], row_match=False)
    assert col(out, "A")["samples"] == ["true"]
    assert col(out, "A")["empty"] == pytest.approx(2 / 3)
    assert all(isinstance(s, str) for s in col(out, "B")["samples"])


def test_short_rows_are_padded_and_empty_rows_are_fine(shop, users):
    out = tools.match_data(users[Role.VIEWER], "master", HEADERS, [["Siam Steel Trading"]])
    assert col(out, "Tax ID")["empty"] == 1.0
    out = tools.match_data(users[Role.VIEWER], "master", HEADERS, [])
    assert out["profile"]["rows"] == 0 and set(out["match"]["columns"]) == set(HEADERS)    # headers only, no samples


# --- recipes ---

@pytest.fixture
def change():
    def make(status):
        user = get_user_model().objects.create_user(username=f"imp-{status}", email=f"imp-{status}@example.com", password="x")
        return Change.objects.create(requester=user, source="web", kind="data", status=status)
    return make


def recipe(schema, approved_change, headers=HEADERS):
    return ImportRecipe.objects.create(
        schema=schema, fingerprint=matcher.fingerprint(headers), headers=headers,
        mapping={"tables": {"Old sheet": {"table": "partner", "columns": {"name": "Vendor Name"}}}},
        filters={"Old sheet": [{"column": "Tax ID", "op": "not_empty"}]}, approved_change=approved_change)


def test_recipe_only_when_its_change_is_applied(shop, users, change):
    r = recipe("master", change(Change.Status.PENDING))
    assert tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS)["recipe"] is None
    r.approved_change = change(Change.Status.APPLIED)
    r.save()
    got = tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS, sheet="Vendors")["recipe"]
    assert got["id"] == r.pk
    assert got["mapping"]["tables"] == {"Vendors": {"table": "partner", "columns": {"name": "Vendor Name"}}}
    assert got["filters"] == {"Vendors": [{"column": "Tax ID", "op": "not_empty"}]}   # re-keyed to this sheet


def test_recipe_only_for_the_same_schema(shop, users, change):
    recipe("crm", change(Change.Status.APPLIED))
    assert tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS)["recipe"] is None
    assert tools.get_import_recipe(users[Role.VIEWER], "master", HEADERS)["recipe"] is None


def test_get_import_recipe_round_trip(shop, users, change):
    out = tools.get_import_recipe(users[Role.VIEWER], "master", ["  tax id", "VENDOR NAME", "Province"])
    assert out == {"recipe": None, "fingerprint": matcher.fingerprint(HEADERS)}
    r = recipe("master", change(Change.Status.APPLIED))
    out = tools.get_import_recipe(users[Role.VIEWER], "master", HEADERS)
    assert out["fingerprint"] == r.fingerprint
    assert out["recipe"]["id"] == r.pk and out["recipe"]["mapping"] == r.mapping and out["recipe"]["filters"] == r.filters
    assert tools.match_data(users[Role.VIEWER], "master", HEADERS, ROWS)["fingerprint"] == r.fingerprint


def test_get_import_recipe_over_several_sheets(shop, users, change):
    a, b = ["Vendor Name", "Tax ID"], ["Province"]
    r = recipe("master", change(Change.Status.APPLIED), headers=a + b)
    out = tools.get_import_recipe(users[Role.VIEWER], "master", sheets=[a, b])
    assert out["recipe"]["id"] == r.pk and out["fingerprint"] == matcher.fingerprint(a + b)
    assert tools.get_import_recipe(users[Role.VIEWER], "master", a)["recipe"] is None


def test_get_import_recipe_bad_inputs(shop, users):
    v = users[Role.VIEWER]
    with pytest.raises(tools.ToolFailure, match="headers"):
        tools.get_import_recipe(v, "master")
    with pytest.raises(tools.ToolFailure, match="headers"):
        tools.get_import_recipe(v, "master", "Vendor Name")
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.get_import_recipe(v, "nope", HEADERS)


# --- nothing kept ---

def test_match_data_stores_nothing(shop, users, change):
    recipe("master", change(Change.Status.APPLIED))
    before = (ImportRun.objects.count(), ImportRecipe.objects.count(), ImportMessage.objects.count(),
              Script.objects.count(), Change.objects.count())
    tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)
    tools.get_import_recipe(users[Role.EDITOR], "master", HEADERS)
    assert (ImportRun.objects.count(), ImportRecipe.objects.count(), ImportMessage.objects.count(),
            Script.objects.count(), Change.objects.count()) == before
    from schema import conn
    with conn.ro_connection() as c:
        assert c.execute("SELECT count(*) AS n FROM master.partner").fetchone()["n"] == 2


def test_web_chat_and_mcp_server_offer_both_tools():
    from chat import toolspecs
    from mcp_server import app
    for name in ("match_data", "get_import_recipe"):
        assert name in toolspecs.SPECS and name in app.INSTRUCTIONS
        assert name in tools.DESCRIPTIONS
    assert "match_data" in str(tools.get_rules(None))
