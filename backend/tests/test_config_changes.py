"""Settings, rules and roles as approvable changes (the same lifecycle as schema changes)."""
import pytest

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from audit.models import AuditLog
from config.siconfig import get_config
from mcp_server import tools
from settings_app.models import Rule, Setting

pytestmark = pytest.mark.django_db


@pytest.fixture
def people():
    return {r: User.objects.create(username=f"{r}@x.com", email=f"{r}@x.com", role=r) for r in (Role.VIEWER, Role.EDITOR, Role.APPROVER)}


def rule(name="noFloat"):
    """A known rule in a known state (the defaults are seeded by migration; make sure this one is enabled and blocking)."""
    r, _ = Rule.objects.update_or_create(name=name, defaults={"severity": "block", "enabled": True})
    return r


def test_proposal_waits_for_an_approver_and_changes_nothing_yet(people):
    get_config.cache_clear()
    before = get_config()["search"]["minConfidence"]
    ch = service.propose(people["editor"], [{"op": "set_setting", "key": "search.minConfidence", "value": 0.6}], source="mcp")
    assert ch.kind == "config" and ch.status == "pending"
    assert ch.diff == [f"~ setting search.minConfidence: {before} → 0.6"]
    assert get_config()["search"]["minConfidence"] == before  # dry run rolled back
    assert not Setting.objects.exists()
    assert not AuditLog.objects.filter(action="settings.updated").exists()


def test_only_approvers_can_decide_not_even_the_requesting_editor(people):
    ch = service.propose(people["editor"], [{"op": "set_setting", "key": "search.topK", "value": 9}], source="mcp")
    assert not service.can_decide(people["editor"], ch)
    with pytest.raises(service.ChangeError) as e:
        service.approve(people["editor"], ch.pk)
    assert e.value.code == "forbidden"
    done = service.approve(people["approver"], ch.pk)
    assert done.status == "applied" and get_config()["search"]["topK"] == 9
    assert AuditLog.objects.filter(action="settings.updated").exists()
    assert AuditLog.objects.filter(action="change.applied", target=f"#{ch.pk}").exists()


def test_it_shows_in_the_approvers_queue_but_not_an_editors(people):
    ch = service.propose(people["editor"], [{"op": "set_setting", "key": "search.topK", "value": 9}], source="mcp")
    assert ch in service.pending_for(people["approver"])
    assert ch not in service.pending_for(people["editor"])


def test_invalid_operations_are_rejected_at_proposal_with_reasons(people):
    ch = service.propose(people["editor"], [
        {"op": "set_setting", "key": "duplicates.threshold", "value": 7},
        {"op": "set_setting", "key": "nope.nope", "value": 1},
        {"op": "update_rule", "name": "ghost", "severity": "warn"},
        {"op": "set_user_role", "email": "nobody@x.com", "role": "editor"},
    ], source="mcp")
    assert ch.status == "rejected"
    assert len(ch.check_result["errors"]) == 4


def test_cannot_mix_config_with_schema_operations(people):
    with pytest.raises(service.ChangeError) as e:
        service.propose(people["approver"], [{"op": "set_setting", "key": "search.topK", "value": 3},
                                             {"op": "create_table", "name": "t", "columns": []}], source="mcp")
    assert e.value.code == "invalid"


def test_rule_update_and_role_change_apply_together_or_not_at_all(people):
    rule()
    ops = [{"op": "update_rule", "name": "noFloat", "severity": "warn"}, {"op": "set_user_role", "email": "viewer@x.com", "role": "editor"}]
    ch = service.propose(people["editor"], ops, source="mcp")
    assert ch.diff == ["~ rule noFloat: severity block → warn", "~ role viewer@x.com: viewer → editor"]
    service.approve(people["approver"], ch.pk)
    assert Rule.objects.get(name="noFloat").severity == "warn"
    assert User.objects.get(email="viewer@x.com").role == Role.EDITOR


def test_create_rule_is_validated_by_the_same_rules_as_the_ui(people):
    bad = service.propose(people["editor"], [{"op": "create_rule", "name": "r", "type": "naming", "params": {"target": "x"}}], source="mcp")
    assert bad.status == "rejected"
    ok = service.propose(people["editor"], [{"op": "create_rule", "name": "tidy", "type": "naming", "severity": "warn",
                                             "params": {"target": "table", "style": "snake_case", "maxLength": 40}}], source="mcp")
    assert ok.status == "pending" and ok.diff == ["+ rule tidy (naming, warn)"]
    service.approve(people["approver"], ok.pk)
    assert Rule.objects.get(name="tidy").severity == "warn"


def test_values_that_moved_since_the_proposal_fail_safely(people):
    ch = service.propose(people["editor"], [{"op": "set_user_role", "email": "editor@x.com", "role": "viewer"}], source="mcp")
    User.objects.filter(pk=people["editor"].pk).update(role=Role.VIEWER)  # changed by someone else meanwhile
    done = service.approve(people["approver"], ch.pk)
    assert done.status == "failed" and "nothing would change" in done.error


def test_applied_config_changes_can_be_reverted(people):
    rule()
    ch = service.propose(people["editor"], [{"op": "update_rule", "name": "noFloat", "enabled": False},
                                            {"op": "set_setting", "key": "search.topK", "value": 11}], source="mcp")
    service.approve(people["approver"], ch.pk)
    assert get_config()["search"]["topK"] == 11 and not Rule.objects.get(name="noFloat").enabled
    rev = service.revert(people["approver"], ch.pk)
    assert rev.status == "applied" and rev.kind == "config"
    assert Rule.objects.get(name="noFloat").enabled and not Setting.objects.filter(key="search.topK").exists()


def test_stale_schema_proposals_are_marked_when_rules_change(people):
    # a rules change invalidates schema proposals checked against the old rules
    from approvals.models import Change as C
    ch = service.propose(people["approver"], [{"op": "create_table", "name": "widget", "columns": [{"name": "label", "type": "text"}]}], source="mcp")
    assert ch.status == "pending"
    rule()
    cc = service.propose(people["approver"], [{"op": "update_rule", "name": "noFloat", "severity": "warn"}], source="mcp")
    service.approve(people["approver"], cc.pk)
    assert service.approve(people["approver"], ch.pk).status == C.Status.STALE


def test_mcp_tools_and_roles(people):
    assert tools.allowed(people["viewer"], "get_settings") and not tools.allowed(people["viewer"], "propose_config_change")
    assert tools.allowed(people["editor"], "propose_config_change") and not tools.allowed(people["editor"], "list_users")
    assert tools.allowed(people["approver"], "list_users")
    out = tools.get_settings(people["viewer"])
    assert any(s["key"] == "search.minConfidence" for s in out["settings"]) and "rules" in out
    res = tools.propose_config_change(people["editor"], [{"op": "set_setting", "key": "search.topK", "value": 8}], "tune search")
    assert res["status"] == "pending" and "approver" in res["next"]
    with pytest.raises(tools.ToolFailure):
        tools.propose_config_change(people["editor"], [{"op": "create_table", "name": "t", "columns": []}])
    assert any(u["email"] == "editor@x.com" for u in tools.list_users(people["approver"])["users"])


def test_events_reach_approvers_live(people):
    """The existing change signal carries config changes too (same serializer, kind 'config')."""
    ch = service.propose(people["editor"], [{"op": "set_setting", "key": "search.topK", "value": 8}], source="mcp")
    data = service.serialize(Change.objects.get(pk=ch.pk))
    assert data["kind"] == "config" and data["status"] == "pending" and data["diff"]
