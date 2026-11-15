"""match_data and get_import_recipe through the real MCP server: listed and callable for editors and up only."""
import pytest

from accounts.models import Role
from search import indexer

from .conftest import (
    fake_vector,  # noqa: F401  (the `embed` fixture comes from conftest)
)
from .test_mcp_http import (  # noqa: F401  (server is a fixture)
    names,
    payload,
    server,
    with_client,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

NEW = {"match_data", "get_import_recipe"}


def test_only_editors_and_approvers_see_them(server, embed):  # noqa: F811
    assert not NEW & set(names(server, Role.VIEWER))
    assert NEW <= set(names(server, Role.EDITOR)) and NEW <= set(names(server, Role.APPROVER))


def test_call_round_trip(server, embed):  # noqa: F811
    from schema import conn
    with conn.rw_connection(autocommit=True) as c:
        c.execute("CREATE TABLE master.partner (partner_id bigserial PRIMARY KEY, name text, tax_id text UNIQUE)")
        c.execute("INSERT INTO master.partner (name, tax_id) VALUES ('Siam Steel', '0105500000002')")
    indexer.reindex(rebuild=True)

    async def go(c):
        m = await c.call_tool("match_data", {"headers": ["Vendor Name", "Tax ID"],
                                             "rows": [["Siam Steel Trading", "0105500000002"], ["New", None]]})
        r = await c.call_tool("get_import_recipe", {"headers": ["Vendor Name", "Tax ID"]})
        return m, r

    m, r = with_client(server["url"], server["tokens"][Role.EDITOR], go)
    m, r = payload(m), payload(r)
    assert m["match"]["columns"]["Vendor Name"][0][:2] == ["partner", "name"]
    assert [e["row"] for e in m["match"]["rows"]["exists"]] == [1]
    assert r["recipe"] is None and len(r["fingerprint"]) == 64

    async def refused(c):
        return await c.call_tool("match_data", {"headers": ["a"], "rows": []})
    out = with_client(server["url"], server["tokens"][Role.VIEWER], refused)
    assert out.is_error and "not available for your role" in out.content[0].text
