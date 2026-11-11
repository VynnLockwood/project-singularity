"""get_rule_history and the rollback_rule config op."""
import pytest

from accounts.models import Role, User
from approvals import service
from mcp_server import tools
from settings_app.models import Rule, RuleVersion
from settings_app import services as rule_services

pytestmark = pytest.mark.django_db


@pytest.fixture
def people():
    return {r: User.objects.create(username=f"{r}@x.com", email=f"{r}@x.com", role=r)
            for r in (Role.VIEWER, Role.EDITOR, Role.APPROVER)}


@pytest.fixture
def rule(people):
    """A rule with two versions: v1 severity block, v2 severity warn."""
    ap = people[Role.APPROVER]
    r = rule_services.create_rule(ap, name="tidy", type="naming", severity="block",
                                  params={"target": "table", "style": "snake_case", "maxLength": 40})
    rule_services.update_rule(ap, r, severity="warn")
    r.refresh_from_db()
    assert r.version == 2
    return r


def apply_ops(people, ops):
    ch = tools.propose_config_change(people[Role.EDITOR], ops)
    assert ch["status"] == "pending", ch
    return service.approve(people[Role.APPROVER], ch["id"])


def test_history_lists_versions_newest_first(people, rule):
    out = tools.get_rule_history(people[Role.VIEWER], "tidy")
    assert out["rule"] == "tidy" and out["current_version"] == 2
    assert [v["version"] for v in out["versions"]] == [2, 1]
    assert out["versions"][0]["snapshot"]["severity"] == "warn"
    assert out["versions"][0]["changed_by"] == "approver@x.com"
    assert out["versions"][1]["action"] == "created"


def test_history_unknown_rule_is_a_tool_failure(people):
    with pytest.raises(tools.ToolFailure) as e:
        tools.get_rule_history(people[Role.VIEWER], "ghost")
    assert "get_rules" in str(e.value)


def test_history_is_a_viewer_tool(people):
    assert tools.TOOL_ROLES["get_rule_history"] == "viewer"
    assert tools.allowed(people[Role.VIEWER], "get_rule_history")


def test_rollback_applies_then_reverts_and_history_only_grows(people, rule):
    done = apply_ops(people, [{"op": "rollback_rule", "name": "tidy", "version": 1}])
    assert done.status == "applied", done.error
    r = Rule.objects.get(name="tidy")
    assert r.severity == "block" and r.version == 3 and RuleVersion.objects.filter(rule=r).count() == 3
    rev = service.revert(people[Role.APPROVER], done.pk)
    assert rev.status == "applied", rev.error
    r.refresh_from_db()
    assert r.severity == "warn" and r.version == 4 and RuleVersion.objects.filter(rule=r).count() == 4


def test_editor_rollback_waits_for_approval(people, rule):
    ch = tools.propose_config_change(people[Role.EDITOR], [{"op": "rollback_rule", "name": "tidy", "version": 1}])
    assert ch["status"] == "pending"
    r = Rule.objects.get(name="tidy")
    assert r.severity == "warn" and r.version == 2
    assert "version 1" in " ".join(ch["diff"])


@pytest.mark.parametrize("op", [
    {"op": "rollback_rule", "name": "ghost", "version": 1},
    {"op": "rollback_rule", "name": "tidy", "version": 99},
    {"op": "rollback_rule", "name": "tidy", "version": "1"},
    {"op": "rollback_rule", "name": "tidy", "version": True},
])
def test_rollback_bad_input_is_a_gate_error(people, rule, op):
    ch = tools.propose_config_change(people[Role.EDITOR], [op])
    assert ch["status"] == "rejected"
    assert Rule.objects.get(name="tidy").version == 2
