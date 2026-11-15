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

def test_both_tools_are_for_editors(users):
    for name in ("match_data", "get_import_recipe"):
        assert tools.TOOL_ROLES[name] == "editor"
        assert name in tools.IMPL
        assert not tools.allowed(users[Role.VIEWER], name)
        assert tools.allowed(users[Role.EDITOR], name) and tools.allowed(users[Role.APPROVER], name)
        assert not tools.allowed(None, name)


# --- profile ---

def test_leading_zero_codes_stay_text(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", ["Code", "Qty"],
                           [["007", 1], ["012", 2.0], ["120", 3]], row_match=False)
    code = col(out, "Code")
    assert code["type"] == "text" and code["samples"] == ["007", "012", "120"]
    assert code["distinct"] == 3 and code["empty"] == 0.0
    qty = col(out, "Qty")
    assert qty["type"] == "integer" and qty["samples"] == ["1", "2", "3"]   # 2.0 read as the number 2
    assert out["profile"]["rows"] == 3


def test_samples_are_at_most_five(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", ["Vendor Name"], [[f"v{i}"] for i in range(20)],
                           row_match=False)
    assert len(col(out, "Vendor Name")["samples"]) == 5


# --- matching ---

def test_vendor_name_ranks_partner_name_first(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS, sheet="Vendors")
    assert out["sheet"] == "Vendors" and out["searchOffline"] is False
    m = out["match"]
    assert m["columns"]["Vendor Name"][0][:2] == ["partner", "name"]
    assert all(len(c) <= 3 for c in m["columns"].values())
    assert m["tables"][0][0] == "partner" and len(m["tables"]) <= 3
    assert m["refs"]["Province"] == {"table": "province", "key": "name"}


def test_exact_business_key_exists(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)
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
    out = tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)
    assert out["searchOffline"] is True
    assert out["match"]["columns"]["Tax ID"][0][:2] == ["partner", "tax_id"]


def test_row_match_false_has_no_rows(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS, row_match=False)
    assert "rows" not in out["match"]


# --- input checks ---

def test_too_many_rows_is_a_clear_error(shop, users):
    cap = tools.MATCH_MAX_ROWS
    with pytest.raises(tools.ToolFailure) as e:
        tools.match_data(users[Role.EDITOR], "master", ["a"], [["x"]] * (cap + 1))
    assert str(cap) in str(e.value) and "sample" in str(e.value) and "begin_load" in str(e.value)
    with pytest.raises(tools.ToolFailure):
        tools.match_data(users[Role.EDITOR], "master", ["a"], [["x"]] * 10, max_rows=5)


def test_bad_inputs(shop, users):
    v = users[Role.EDITOR]
    with pytest.raises(tools.ToolFailure, match="headers"):
        tools.match_data(v, "master", [], [])
    with pytest.raises(tools.ToolFailure, match="rows"):
        tools.match_data(v, "master", ["a"], "nope")
    with pytest.raises(tools.ToolFailure, match="rows"):
        tools.match_data(v, "master", ["a"], [{"a": 1}])
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.match_data(v, "nope", ["a"], [["x"]])


def test_duplicate_and_blank_headers_are_renamed_like_the_reader(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", ["Name", "Name", None, "  ", " Code "],
                           [["a", "b", "c", "d", "e"]], row_match=False)
    assert out["headers"] == ["Name", "Name_2", "column_3", "column_4", "Code"]
    assert [c["header"] for c in out["profile"]["columns"]] == out["headers"]
    assert set(out["match"]["columns"]) == set(out["headers"])
    assert any("Name_2" in n for n in out["notes"]) and any("column_3" in n for n in out["notes"])


def test_odd_cell_values_become_text(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", ["A", "B"],
                           [[True, {"x": 1}], [None, [1, 2]], ["", 3]], row_match=False)
    assert col(out, "A")["samples"] == ["true"]
    assert col(out, "A")["empty"] == pytest.approx(2 / 3)
    assert all(isinstance(s, str) for s in col(out, "B")["samples"])


def test_short_rows_are_padded_and_empty_rows_are_fine(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", HEADERS, [["Siam Steel Trading"]])
    assert col(out, "Tax ID")["empty"] == 1.0
    out = tools.match_data(users[Role.EDITOR], "master", HEADERS, [])
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
    assert tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)["recipe"] is None
    r.approved_change = change(Change.Status.APPLIED)
    r.save()
    got = tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS, sheet="Vendors")["recipe"]
    assert got["id"] == r.pk
    assert got["mapping"]["tables"] == {"Vendors": {"table": "partner", "columns": {"name": "Vendor Name"}}}
    assert got["filters"] == {"Vendors": [{"column": "Tax ID", "op": "not_empty"}]}   # re-keyed to this sheet


def test_recipe_only_for_the_same_schema(shop, users, change):
    recipe("crm", change(Change.Status.APPLIED))
    assert tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)["recipe"] is None
    assert tools.get_import_recipe(users[Role.EDITOR], "master", HEADERS)["recipe"] is None


def test_get_import_recipe_round_trip(shop, users, change):
    out = tools.get_import_recipe(users[Role.EDITOR], "master", ["  tax id", "VENDOR NAME", "Province"])
    assert out == {"recipe": None, "fingerprint": matcher.fingerprint(HEADERS)}
    r = recipe("master", change(Change.Status.APPLIED))
    out = tools.get_import_recipe(users[Role.EDITOR], "master", HEADERS)
    assert out["fingerprint"] == r.fingerprint
    assert out["recipe"]["id"] == r.pk and out["recipe"]["mapping"] == r.mapping and out["recipe"]["filters"] == r.filters
    assert tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)["fingerprint"] == r.fingerprint


def test_get_import_recipe_over_several_sheets(shop, users, change):
    a, b = ["Vendor Name", "Tax ID"], ["Province"]
    r = recipe("master", change(Change.Status.APPLIED), headers=a + b)
    out = tools.get_import_recipe(users[Role.EDITOR], "master", sheets=[a, b])
    assert out["recipe"]["id"] == r.pk and out["fingerprint"] == matcher.fingerprint(a + b)
    assert tools.get_import_recipe(users[Role.EDITOR], "master", a)["recipe"] is None


def test_get_import_recipe_bad_inputs(shop, users):
    v = users[Role.EDITOR]
    with pytest.raises(tools.ToolFailure, match="headers"):
        tools.get_import_recipe(v, "master")
    with pytest.raises(tools.ToolFailure, match="headers"):
        tools.get_import_recipe(v, "master", "Vendor Name")
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.get_import_recipe(v, "nope", HEADERS)


# --- nothing kept ---

def test_match_data_stores_nothing(shop, users, change):
    recipe("master", change(Change.Status.APPLIED))
    def counts():
        from audit.models import AuditLog
        from duplicates.models import DuplicateGroup
        return (ImportRun.objects.count(), ImportRecipe.objects.count(), ImportMessage.objects.count(),
                Script.objects.count(), Change.objects.count(), DuplicateGroup.objects.count(),
                AuditLog.objects.count())
    before = counts()
    tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS)
    tools.get_import_recipe(users[Role.EDITOR], "master", HEADERS)
    assert counts() == before
    from schema import conn
    with conn.ro_connection() as c:
        assert c.execute("SELECT count(*) AS n FROM master.partner").fetchone()["n"] == 2


def test_mcp_server_offers_both_tools_and_the_web_chat_does_not(users):
    from chat import toolspecs
    from mcp_server import app
    for name in ("match_data", "get_import_recipe"):
        assert name in app.INSTRUCTIONS and name in tools.DESCRIPTIONS
        assert name not in toolspecs.SPECS and name in toolspecs.NOT_IN_CHAT
        assert name not in {s["function"]["name"] for s in toolspecs.specs_for(users[Role.APPROVER])}
    assert "match_data" in str(tools.get_rules(None))


# --- input caps (fix round 1) ---

def test_rows_wider_than_headers_are_refused(shop, users):
    with pytest.raises(tools.ToolFailure) as e:
        tools.match_data(users[Role.EDITOR], "master", ["a"], [["1"] * 1_000])
    assert "row 1" in str(e.value) and "1 header" in str(e.value)


def test_too_many_cells_is_a_clear_error(shop, users):
    cols = tools.MATCH_MAX_CELLS // 1_000 + 1
    with pytest.raises(tools.ToolFailure) as e:
        tools.match_data(users[Role.EDITOR], "master", [f"c{i}" for i in range(cols)], [["x"] * cols] * 1_000)
    msg = str(e.value)
    assert str(tools.MATCH_MAX_CELLS) in msg and "sample" in msg and "begin_load" in msg


def test_long_cells_are_cut_before_matching(shop, users, monkeypatch):
    import time
    seen = []
    real = matcher.match

    def spy(schema, sources, profiles, row_match):
        seen.extend(sources)
        return real(schema, sources, profiles, row_match)
    monkeypatch.setattr(matcher, "match", spy)
    long_name = "Acme Company Limited " * 10_000
    t = time.monotonic()
    tools.match_data(users[Role.EDITOR], "master", ["Vendor Name" * 100, "Tax ID"],
                     [[long_name, "0105500000099"]] * 200)
    assert time.monotonic() - t < 20
    cells = [c for r in seen[0]["rows"] for c in r] + seen[0]["headers"]
    assert max(len(c) for c in cells) == tools.MATCH_MAX_CELL == 500


def test_unprintable_values_do_not_break_the_tool(shop, users):
    import sys
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(4300)
    out = tools.match_data(users[Role.EDITOR], "master", ["A"], [[10 ** 5000], ["ok"]], row_match=False)
    a = col(out, "A")
    assert a["distinct"] == 2 and all(isinstance(s, str) and len(s) <= 500 for s in a["samples"])


def test_non_string_headers_become_text(shop, users):
    out = tools.match_data(users[Role.EDITOR], "master", [2024, 1.0, True, {"x": 1}], [["a", "b", "c", "d"]],
                           row_match=False)
    assert out["headers"] == ["2024", "1", "true", '{"x": 1}']
    assert tools.get_import_recipe(users[Role.EDITOR], "master", [2024, 1.0])["fingerprint"] == \
        matcher.fingerprint(["2024", "1"])


def test_operator_switch_turns_row_matching_off(shop, users, monkeypatch):
    from config import siconfig
    real = siconfig.get_config

    def cfg():
        c = real()
        return {**c, "import": {**c["import"], "rowMatch": False}}
    monkeypatch.setattr(tools, "get_config", cfg)
    assert "rows" not in tools.match_data(users[Role.EDITOR], "master", HEADERS, ROWS, row_match=True)["match"]


def test_rekey_recipe_lives_in_the_matcher():
    from intake import run
    assert run._rekey_recipe is matcher.rekey_recipe
