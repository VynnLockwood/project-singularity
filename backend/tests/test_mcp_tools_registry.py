"""Guards the tool registry: a tool cannot be half-added."""
import asyncio

from chat import toolspecs
from mcp_server import tools

NOT_IN_CHAT = {"approve_change", "reject_change"}   # the person decides in the web app, never the model


def test_every_tool_has_a_role_and_an_implementation():
    assert set(tools.TOOL_ROLES) == set(tools.IMPL)


def test_every_tool_is_registered_with_the_mcp_server():
    from mcp_server.app import mcp
    registered = {t.name for t in asyncio.run(super(type(mcp), mcp).list_tools())}
    assert registered == set(tools.TOOL_ROLES)


def test_the_web_chat_has_every_tool_except_deciding():
    assert set(toolspecs.SPECS) == set(tools.TOOL_ROLES) - NOT_IN_CHAT


def test_clamp():
    assert tools.clamp(None, 20, 100) == 20
    assert tools.clamp(0, 20, 100) == 20
    assert tools.clamp(-5, 20, 100) == 20
    assert tools.clamp("abc", 20, 100) == 20
    assert tools.clamp(True, 20, 100) == 20
    assert tools.clamp(7, 20, 100) == 7
    assert tools.clamp(10**9, 20, 100) == 100
