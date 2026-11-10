"""MCP tool read_rows: one table, a page at a time, without SQL."""
import pytest

from accounts.models import Role
from mcp_server import tools

from .mcp_helpers import applied, create_table, make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

NAMES = ["Bangkok", "Samut Prakan", "Nonthaburi", "Pathum Thani", "Ayutthaya", "Ang Thong", "Lopburi", "Sing Buri",
         "Chai Nat", "Saraburi", "Chon Buri", "Rayong", "Chanthaburi", "Trat", "Chachoengsao", "Prachin Buri",
         "Nakhon Nayok", "Sa Kaeo", "Nakhon Ratchasima", "Buri Ram", "Surin", "Si Sa Ket", "Ubon Ratchathani",
         "Yasothon", "Chaiyaphum", "Amnat Charoen", "Bueng Kan", "Nong Bua Lam Phu", "Khon Kaen", "Udon Thani"]


@pytest.fixture
def users():
    u = make_users()
    applied(u[Role.APPROVER], [create_table("province", "name_en")])
    applied(u[Role.APPROVER], [{"op": "insert_rows", "table": "province", "rows": [{"name_en": n} for n in NAMES]}])
    return u


def test_first_page_and_total(users):
    v = users[Role.VIEWER]
    out = tools.read_rows(v, "province")
    assert out["total"] == 30 and len(out["rows"]) == 30 and out["truncated"] is False
    assert out["primaryKey"] == "province_id" and "name_en" in [c["name"] for c in out["columns"]]
    page = tools.read_rows(v, "province", limit=10)
    assert len(page["rows"]) == 10 and page["truncated"] is True and page["total"] == 30


def test_offset_sort_and_filter(users):
    v = users[Role.VIEWER]
    out = tools.read_rows(v, "province", offset=10, limit=10, sort="name_en", direction="desc")
    assert [r["name_en"] for r in out["rows"]] == sorted(NAMES, reverse=True)[10:20]
    last = tools.read_rows(v, "province", offset=25, limit=10)
    assert len(last["rows"]) == 5 and last["truncated"] is False
    buri = tools.read_rows(v, "province", filters={"name_en": "BURI"})
    assert buri["total"] == len([n for n in NAMES if "buri" in n.lower()]) and all("buri" in r["name_en"].lower() for r in buri["rows"])


def test_limits_are_clamped(users):
    v = users[Role.VIEWER]
    assert len(tools.read_rows(v, "province", limit=10**6)["rows"]) == 30      # capped at 200, table has 30
    assert tools.read_rows(v, "province", limit=10**6)["limit"] == 200
    assert tools.read_rows(v, "province", limit=0)["limit"] == 50
    assert tools.read_rows(v, "province", offset=-4)["offset"] == 0
    assert tools.read_rows(v, "province", offset="x")["offset"] == 0


def test_unknown_table_schema_column(users):
    v = users[Role.VIEWER]
    with pytest.raises(tools.ToolFailure, match="list_tables"):
        tools.read_rows(v, "nope")
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.read_rows(v, "province", schema="nope")
    with pytest.raises(tools.ToolFailure, match="unknown column 'nope'.*describe_table"):
        tools.read_rows(v, "province", sort="nope")
    with pytest.raises(tools.ToolFailure, match="unknown column"):
        tools.read_rows(v, "province", filters={"nope": "x"})
    with pytest.raises(tools.ToolFailure, match="filters"):
        tools.read_rows(v, "province", filters="text")
    with pytest.raises(tools.ToolFailure, match="direction"):
        tools.read_rows(v, "province", direction="sideways")


def test_a_viewer_may_read_and_inactive_rows_are_hidden_by_default(users):
    v, ap = users[Role.VIEWER], users[Role.APPROVER]
    assert tools.allowed(v, "read_rows")
    applied(ap, [{"op": "deactivate_rows", "table": "province", "where": {"name_en": "Trat"}}])
    assert tools.read_rows(v, "province")["total"] == 29
    assert tools.read_rows(v, "province", include_inactive=True)["total"] == 30
