import hashlib
import json

from django.db import transaction

from audit.models import record
from gate.defaults import DEFAULT_RULES
from gate.rules import validate_rule_definition

from .models import Rule, RuleVersion


class RuleError(Exception):
    def __init__(self, errors):
        errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(errors))
        self.errors = errors


def _require_approver(actor):
    if not actor.can_approve:
        raise RuleError("only approvers can change rules")


def _save_version(rule: Rule, actor, action: str):
    RuleVersion.objects.create(
        rule=rule, version=rule.version, snapshot=rule.snapshot(), action=action, changed_by=actor
    )


@transaction.atomic
def create_rule(actor, *, name, type, params, severity="block", enabled=True, description="") -> Rule:
    _require_approver(actor)
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise RuleError("name is required (max 64 characters)")
    if Rule.objects.filter(name=name).exists():
        raise RuleError(f"a rule named {name!r} already exists")
    problems = validate_rule_definition(type, params, severity)
    if problems:
        raise RuleError(problems)
    rule = Rule.objects.create(
        name=name, type=type, params=params, severity=severity, enabled=enabled,
        description=description, updated_by=actor,
    )
    _save_version(rule, actor, "created")
    record(actor, "rule.created", target=name, details=rule.snapshot())
    return rule


@transaction.atomic
def update_rule(actor, rule: Rule, **fields) -> Rule:
    _require_approver(actor)
    allowed = {"params", "severity", "enabled", "description"}
    unknown = set(fields) - allowed
    if unknown:
        raise RuleError(f"cannot change {sorted(unknown)}")
    before = rule.snapshot()
    for k, v in fields.items():
        setattr(rule, k, v)
    problems = validate_rule_definition(rule.type, rule.params, rule.severity)
    if problems:
        raise RuleError(problems)
    if rule.snapshot() == before:
        return rule
    rule.version += 1
    rule.updated_by = actor
    rule.save()
    _save_version(rule, actor, "updated")
    record(actor, "rule.updated", target=rule.name, details={"before": before, "after": rule.snapshot()})
    return rule


@transaction.atomic
def rollback_rule(actor, rule: Rule, version: int) -> Rule:
    _require_approver(actor)
    old = RuleVersion.objects.filter(rule=rule, version=version).first()
    if old is None:
        raise RuleError(f"version {version} does not exist")
    before = rule.snapshot()
    snap = old.snapshot
    rule.params, rule.severity = snap["params"], snap["severity"]
    rule.enabled, rule.description = snap["enabled"], snap["description"]
    rule.version += 1  # history only moves forward
    rule.updated_by = actor
    rule.save()
    _save_version(rule, actor, "rolled_back")
    record(actor, "rule.rolled_back", target=rule.name,
           details={"to_version": version, "before": before, "after": rule.snapshot()})
    return rule


def active_rules() -> list[dict]:
    """Rules in the shape gate.rules.evaluate expects."""
    return [
        {**r.snapshot(), "id": r.pk, "version": r.version}
        for r in Rule.objects.all().order_by("id")
    ]


def rules_hash() -> str:
    """Changes whenever any rule (or its enabled flag) changes; used to detect stale proposals."""
    payload = [(r["name"], r["type"], r["params"], r["severity"], r["enabled"]) for r in active_rules()]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def seed_default_rules() -> int:
    """Install the default rules, once, when there are none. Returns how many were created."""
    if Rule.objects.exists():
        return 0
    with transaction.atomic():
        for d in DEFAULT_RULES:
            rule = Rule.objects.create(**d)
            _save_version(rule, None, "created")
    return len(DEFAULT_RULES)
