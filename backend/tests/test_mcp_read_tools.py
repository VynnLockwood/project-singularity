"""profile_column and lookup_options: read-only, any role, no SQL through names."""
import pytest

from accounts.models import Role
from mcp_server import tools
from schema import rows as rows_mod

from .mcp_helpers import applied, make_users

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

LONG = "x" * 200
NAMES = (["alpha"] * 4 + ["beta"] * 3 + ["gamma"] * 2 + [None] * 2 + [LONG] * 2 + [f"u{i}" for i in range(12)])


@pytest.fixture
def users():
    u = make_users()
    ap = u[Role.APPROVER]
    applied(ap, [{"op": "create_table", "name": "supplier", "columns": [
        {"name": "name", "type": "text"}, {"name": "rating", "type": "integer"}, {"name": "meta", "type": "jsonb"}]}])
    rows = []
    for i, n in enumerate(NAMES):
        row = {"rating": 100 + i if n == "gamma" else i + 1}
        if n is not None:
            row["name"] = n
        rows.append(row)
    applied(ap, [{"op": "insert_rows", "table": "supplier", "rows": rows}])
    applied(ap, [{"op": "deactivate_rows", "table": "supplier", "where": {"name": "gamma"}}])
    return u


def test_profile_counts_only_active_rows(users):
    out = tools.profile_column(users[Role.VIEWER], "supplier", "name")
    assert out["total"] == 23 and out["null_count"] == 2
    assert out["distinct_count"] == 15                    # gamma is deactivated, so it is not counted


def test_profile_top_values_are_capped_ordered_and_cut(users):
    top = tools.profile_column(users[Role.VIEWER], "supplier", "name")["top_values"]
    assert len(top) == 10
    assert top[:3] == [{"value": "alpha", "count": 4}, {"value": "beta", "count": 3}, {"value": "x" * 80, "count": 2}]
    assert all(len(t["value"]) <= 80 for t in top)
    assert "gamma" not in {t["value"] for t in top}


def test_profile_min_max_only_for_orderable_types(users):
    r = tools.profile_column(users[Role.VIEWER], "supplier", "rating")

    ratings = [i + 1 for i, n in enumerate(NAMES) if n != "gamma"]
    assert (r["min"], r["max"]) == (min(ratings), max(ratings))
    m = tools.profile_column(users[Role.VIEWER], "supplier", "meta")
    assert m["min"] is None and m["max"] is None and m["null_count"] == m["total"]


@pytest.mark.parametrize("table,column", [
    ("supplier", "name; drop table supplier"), ("supplier", '"name"'), ("supplier", "name--"), ("supplier", "nope"),
    ("supplier; select 1", "name"), ("nope", "name"), ("pg_catalog.pg_class", "relname"),
])
def test_profile_cannot_be_pointed_at_sql_through_names(users, table, column):
    with pytest.raises(tools.ToolFailure, match="list_tables|describe_table"):
        tools.profile_column(users[Role.VIEWER], table, column)
    assert tools.read_rows(users[Role.VIEWER], "supplier")["total"] == 23         # nothing was dropped


def test_profile_unknown_schema_reads_nothing(users):
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.profile_column(users[Role.VIEWER], "supplier", "name", schema="nope")


def test_profile_and_options_are_viewer_tools(users):
    for name in ("profile_column", "lookup_options"):
        assert tools.TOOL_ROLES[name] == "viewer" and tools.allowed(users[Role.VIEWER], name)


def test_lookup_options_matches_the_web_options(users):
    out = tools.lookup_options(users[Role.VIEWER], "supplier", q="u1", limit=2)
    web = rows_mod.options("supplier", q="u1", limit=2)
    assert out == {"label_column": web["labelColumn"], "truncated": web["truncated"], "options": web["options"]}
    assert out["label_column"] == "name" and out["truncated"] is True and len(out["options"]) == 2
    assert all("u1" in o["label"] for o in out["options"])


def test_lookup_options_limit_is_capped(users):
    out = tools.lookup_options(users[Role.VIEWER], "supplier", limit=10**6)
    assert len(out["options"]) <= rows_mod.MAX_PAGE


def test_lookup_options_errors_point_at_the_listing_tools(users):
    with pytest.raises(tools.ToolFailure, match="list_tables"):
        tools.lookup_options(users[Role.VIEWER], "nope")
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.lookup_options(users[Role.VIEWER], "supplier", schema="nope")
