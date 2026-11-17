import pytest
from django.http import JsonResponse
from django.test import RequestFactory

from accounts.api import endpoint
from accounts.models import ROLE_RANK, Role, User
from mcp_server import tools


def make_user(email, role):
    return User.objects.create(username=email, email=email, role=role)


@pytest.mark.django_db
def test_admin_ranks_above_approver():
    admin = make_user("a@example.com", Role.ADMIN)
    approver = make_user("b@example.com", Role.APPROVER)
    assert ROLE_RANK[Role.ADMIN] == 3 > ROLE_RANK[Role.APPROVER]
    assert admin.can_approve and admin.can_admin
    assert approver.can_approve and not approver.can_admin


@pytest.mark.django_db
def test_endpoint_admin_min_role():
    @endpoint("GET", min_role="admin")
    def view(request):
        return JsonResponse({"ok": True})

    def call(user):
        request = RequestFactory().get("/x")
        request.user = user
        return view(request).status_code

    assert call(make_user("b@example.com", Role.APPROVER)) == 403
    assert call(make_user("a@example.com", Role.ADMIN)) == 200


@pytest.mark.django_db
def test_admin_sees_approver_tools_over_mcp():
    admin = make_user("a@example.com", Role.ADMIN)
    approver = make_user("b@example.com", Role.APPROVER)
    assert tools.RANK["admin"] == 3
    names = list(tools.TOOL_ROLES)
    assert {"approve_change", "revert_change", "list_users"} <= {n for n in names if tools.allowed(admin, n)}
    # admin = every approver tool, plus only the per-database grant tools (admin-only by design)
    admin_only = [n for n in names if tools.allowed(admin, n) and not tools.allowed(approver, n)]
    assert admin_only == ["get_user_access", "set_user_access"]
    assert [n for n in names if tools.allowed(approver, n)] == [n for n in names if tools.allowed(admin, n)
                                                               and n not in admin_only]
