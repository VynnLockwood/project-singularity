"""MCP tool export_schema: a database as SQL, inline up to a size limit."""
import pytest

from accounts.models import Role
from audit.models import AuditLog
from mcp_server import tools
from settings_app import tunables

from .mcp_helpers import applied, create_table, make_users


def test_cut_sql_leaves_small_output_alone():
    assert tools.cut_sql(b"CREATE TABLE a ();\n", 100) == (b"CREATE TABLE a ();\n", False)
    exact = b"CREATE TABLE a ();\n"
    assert tools.cut_sql(exact, len(exact)) == (exact, False)


def test_cut_sql_cuts_at_the_end_of_a_statement():
    text = b"CREATE TABLE a ();\nCREATE TABLE b ();\nCREATE TABLE c ();\n"
    cut, truncated = tools.cut_sql(text, len(text) - 1)
    assert truncated is True and cut == b"CREATE TABLE a ();\nCREATE TABLE b ();\n"
    data = b"COPY t (a) FROM stdin;\n1\n2\n\\.\nCOPY u (a) FROM stdin;\n3\n\\.\n"
    cut, truncated = tools.cut_sql(data, len(data) - 2)
    assert truncated is True and cut.endswith(b"\\.\n") and b"COPY u" not in cut
    tricky = b"COPY t (a) FROM stdin;\nends with a semicolon;\nsecond row\n\\.\n"
    assert tools.cut_sql(tricky, len(tricky) - 3) == (b"", True)      # never inside a COPY block


def test_cut_sql_does_not_cut_inside_a_quoted_comment():
    text = (b"CREATE TABLE a ();\n"
            b"COMMENT ON TABLE a IS 'first line;\nsecond line; it''s still the comment;\nthird';\n"
            b"CREATE TABLE b ();\n")
    for cap in range(len(b"CREATE TABLE a ();\n") + 1, len(text) - len(b"CREATE TABLE b ();\n")):
        cut, truncated = tools.cut_sql(text, cap)
        assert truncated is True and cut == b"CREATE TABLE a ();\n", cap      # never ends inside the string
    whole_comment = len(text) - len(b"CREATE TABLE b ();\n")
    assert tools.cut_sql(text, whole_comment)[0].endswith(b"third';\n")


def test_cut_sql_never_returns_half_a_statement():
    assert tools.cut_sql(b"CREATE TABLE a_very_long_name ();\n", 10) == (b"", True)


@pytest.fixture
def users(master):
    u = make_users()
    ap = u[Role.APPROVER]
    applied(ap, [create_table("region"), create_table("province")])
    applied(ap, [{"op": "insert_rows", "table": "region", "rows": [{"name_en": "Central"}, {"name_en": "East"}]}])
    return u


@pytest.mark.django_db(transaction=True)
def test_structure_export(users):
    out = tools.export_schema(users[Role.VIEWER])
    assert out["truncated"] is False and out["note"] == "" and out["filename"].endswith(".sql")
    assert "CREATE TABLE" in out["sql"] and "region" in out["sql"] and "province" in out["sql"]
    assert "COPY" not in out["sql"] and out["bytes"] == len(out["sql"].encode())


@pytest.mark.django_db(transaction=True)
def test_data_needs_an_editor(users):
    with pytest.raises(tools.ToolFailure, match="editor"):
        tools.export_schema(users[Role.VIEWER], include_data=True)
    out = tools.export_schema(users[Role.EDITOR], include_data=True)
    assert "INSERT INTO" in out["sql"] and "Central" in out["sql"]


@pytest.mark.django_db(transaction=True)
def test_empty_database_exports(master):
    out = tools.export_schema(make_users()[Role.VIEWER])
    assert out["truncated"] is False and out["sql"].strip() != "" and "CREATE TABLE" not in out["sql"]


@pytest.mark.django_db(transaction=True)
def test_truncates_at_the_setting(users):
    tunables.apply(users[Role.APPROVER], {"limits.maxInlineExportBytes": 1024})
    out = tools.export_schema(users[Role.EDITOR], include_data=True)
    assert out["truncated"] is True and len(out["sql"].encode()) <= 1024
    assert "web app" in out["note"]
    assert out["sql"] == "" or out["sql"].endswith(";\n") or out["sql"].endswith("\\.\n")


@pytest.mark.django_db(transaction=True)
def test_unknown_schema(users):
    with pytest.raises(tools.ToolFailure, match="list_schemas"):
        tools.export_schema(users[Role.VIEWER], schema="nope")


@pytest.mark.django_db(transaction=True)
def test_export_is_audited(users):
    tools.export_schema(users[Role.EDITOR], include_data=True)
    entry = AuditLog.objects.filter(action="schema.export").latest("id")
    assert entry.target == "master" and entry.details["data"] is True and entry.details["via"] == "mcp"
    assert entry.actor_email == "editor@example.com"


@pytest.mark.django_db(transaction=True)
def test_tunable_is_listed(users):
    t = next(x for x in tools.get_settings(users[Role.VIEWER])["settings"] if x["key"] == "limits.maxInlineExportBytes")
    assert t["default"] == 200000 and t["min"] == 1024 and t["max"] == 5000000
