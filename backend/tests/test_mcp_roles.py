"""MCP tokens act with the user's real role, checked on every call: deciding changes, turning users off, role changes."""
import pytest

from accounts import tokens
from accounts.models import Role, User
from accounts.services import change_role, set_user_active
from approvals import service
from approvals.models import Change
from mcp_server import tools

from .mcp_helpers import create_table
from .test_mcp_http import payload, server, with_client  # noqa: F401  (server is a fixture)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


def call(server, user_or_role, tool, args):
    token = server["tokens"][user_or_role]
    return with_client(server["url"], token, lambda c: c.call_tool(tool, args))


@pytest.fixture
def srv(server):  # noqa: F811
    admin = User.objects.create(username="admin@example.com", email="admin@example.com", role=Role.ADMIN)
    server["users"][Role.ADMIN] = admin
    server["tokens"][Role.ADMIN] = tokens.create_token(admin, "e2e")[1]
    return server


def data_change(srv, name="widget"):
    ch = tools.propose_change(srv["users"][Role.EDITOR], [create_table(name)])
    assert ch["status"] == "pending", ch
    return ch["id"]


def role_change(srv):
    target = User.objects.create(username="t@example.com", email="t@example.com", role=Role.VIEWER)
    ch = tools.propose_config_change(srv["users"][Role.EDITOR], [{"op": "set_user_role", "email": target.email, "role": "editor"}])
    assert ch["status"] == "pending", ch
    return ch["id"], target


def status(pk):
    return Change.objects.get(pk=pk).status


@pytest.mark.parametrize("role", [Role.EDITOR, Role.VIEWER])
def test_editor_and_viewer_tokens_cannot_approve_reject_revert(srv, role):
    pk = data_change(srv)
    for tool in ("approve_change", "reject_change", "revert_change"):
        res = call(srv, role, tool, {"change_id": pk})
        assert res.is_error and "not available for your role" in res.content[0].text, tool
    assert status(pk) == "pending"


def test_approver_token_approves_data_change(srv):
    pk = data_change(srv)
    res = call(srv, Role.APPROVER, "approve_change", {"change_id": pk})
    assert not res.is_error and payload(res)["status"] == "applied"


def test_approver_token_cannot_approve_role_change(srv):
    pk, target = role_change(srv)
    res = call(srv, Role.APPROVER, "approve_change", {"change_id": pk})
    assert res.is_error
    assert status(pk) == "pending"
    target.refresh_from_db()
    assert target.role == Role.VIEWER


def test_admin_token_approves_role_change(srv):
    pk, target = role_change(srv)
    res = call(srv, Role.ADMIN, "approve_change", {"change_id": pk})
    assert not res.is_error and payload(res)["status"] == "applied"
    target.refresh_from_db()
    assert target.role == Role.EDITOR


def test_turned_off_user_token_refused_next_call(srv):
    import urllib.error
    assert not call(srv, Role.EDITOR, "get_rules", {}).is_error
    admin = srv["users"][Role.ADMIN]
    set_user_active(admin, srv["users"][Role.EDITOR], False)
    import urllib.request
    req = urllib.request.Request(srv["url"], data=b"{}", headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {srv['tokens'][Role.EDITOR]}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401
    with pytest.raises(Exception):
        call(srv, Role.EDITOR, "get_rules", {})


def test_role_change_applies_next_call(srv):
    ch = call(srv, Role.EDITOR, "propose_change", {"operations": [create_table("gadget")]})
    assert not ch.is_error
    change_role(srv["users"][Role.ADMIN], srv["users"][Role.EDITOR], Role.VIEWER)
    res = call(srv, Role.EDITOR, "propose_change", {"operations": [create_table("gizmo")]})
    assert res.is_error and "not available for your role" in res.content[0].text


def test_deleted_users_token_does_not_authenticate(srv):
    raw = srv["tokens"][Role.EDITOR]
    assert tokens.authenticate(raw) is not None
    srv["users"][Role.EDITOR].delete()
    assert tokens.authenticate(raw) is None
