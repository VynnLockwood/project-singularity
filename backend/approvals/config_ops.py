"""Configuration changes as operations: settings, rules and roles.

These are the important knobs, so when an AI client wants one changed it proposes it like any other change:
it waits in the approval queue (approvers only decide), shows a diff, and can be reverted. The work itself
is done by the same services the Settings tab uses, so every validation rule is shared.

  set_setting{key,value}   reset_setting{key}
  update_rule{name, severity?, enabled?, params?, description?}   create_rule{name,type,params,severity?,enabled?,description?}
  set_user_role{email,role}
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from accounts import services as account_services
from accounts.models import User
from config.siconfig import get_config, invalidate_overrides
from settings_app import services as rule_services
from settings_app import tunables
from settings_app.models import Rule, Setting

CONFIG_OPS = frozenset({"set_setting", "reset_setting", "update_rule", "create_rule", "set_user_role"})
MAX_OPS = 20
RULE_FIELDS = ("severity", "enabled", "params", "description")


@dataclass
class Result:
    diff: list[str] = field(default_factory=list)
    reverse: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _Rollback(Exception):
    pass


def is_config(raw) -> bool:
    return isinstance(raw, list) and bool(raw) and all(isinstance(o, dict) and o.get("op") in CONFIG_OPS for o in raw)


def has_config(raw) -> bool:
    return isinstance(raw, list) and any(isinstance(o, dict) and o.get("op") in CONFIG_OPS for o in raw)


def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _setting(res: Result, actor, key, new):
    if key not in tunables.TUNABLES:
        res.errors.append(f"unknown setting {key!r}")
        return
    section, name = key.split(".")
    before = get_config()[section][name]
    was_overridden = Setting.objects.filter(key=key).exists()
    try:
        tunables.apply(actor, {key: new})
    except tunables.TunableError as exc:
        res.errors += exc.errors
        return
    after = get_config()[section][name]
    if after == before and was_overridden == (new is not None):
        return
    res.diff.append(f"~ setting {key}: {_short(before)} → {_short(after)}" + (" (reset to default)" if new is None else ""))
    res.reverse.append({"op": "set_setting", "key": key, "value": before} if was_overridden else {"op": "reset_setting", "key": key})


def _update_rule(res: Result, actor, op):
    rule = Rule.objects.filter(name=op.get("name")).first()
    if rule is None:
        res.errors.append(f"no rule named {op.get('name')!r}")
        return
    fields = {k: op[k] for k in RULE_FIELDS if k in op}
    if not fields:
        res.errors.append(f"update_rule {rule.name}: give at least one of {list(RULE_FIELDS)}")
        return
    before = rule.snapshot()
    try:
        rule_services.update_rule(actor, rule, **fields)
    except rule_services.RuleError as exc:
        res.errors += [f"rule {rule.name}: {e}" for e in exc.errors]
        return
    rule.refresh_from_db()
    after = rule.snapshot()
    changed = [k for k in RULE_FIELDS if before[k] != after[k]]
    if not changed:
        return
    parts = [f"{k} {_short(before[k])} → {_short(after[k])}" if k != "params" else "parameters changed" for k in changed]
    res.diff.append(f"~ rule {rule.name}: " + "; ".join(parts))
    res.reverse.append({"op": "update_rule", "name": rule.name, **{k: before[k] for k in changed}})


def _create_rule(res: Result, actor, op):
    try:
        rule = rule_services.create_rule(
            actor, name=op.get("name"), type=op.get("type"), params=op.get("params"),
            severity=op.get("severity", "block"), enabled=op.get("enabled", True), description=str(op.get("description", "")),
        )
    except rule_services.RuleError as exc:
        res.errors += [f"new rule: {e}" for e in exc.errors]
        return
    res.diff.append(f"+ rule {rule.name} ({rule.type}, {rule.severity})")
    res.reverse.append({"op": "update_rule", "name": rule.name, "enabled": False})  # rules are never deleted; undo = switch off


def _set_role(res: Result, actor, op):
    target = User.objects.filter(email__iexact=str(op.get("email", ""))).first()
    if target is None:
        res.errors.append(f"no user with email {op.get('email')!r}")
        return
    before = target.role
    try:
        account_services.change_role(actor, target, str(op.get("role", "")))
    except account_services.RoleChangeDenied as exc:
        res.errors.append(f"{target.email}: {exc}")
        return
    if before != target.role:
        res.diff.append(f"~ role {target.email}: {before} → {target.role}")
        res.reverse.append({"op": "set_user_role", "email": target.email, "role": before})


def run(ops, actor, *, dry_run: bool) -> Result:
    """Apply the operations in one transaction. With dry_run, or on any error, nothing is kept."""
    res = Result()
    if not isinstance(ops, list) or not ops:
        res.errors.append("operations must be a non-empty list")
        return res
    if len(ops) > MAX_OPS:
        res.errors.append(f"too many operations (max {MAX_OPS})")
        return res
    try:
        with transaction.atomic():
            for i, op in enumerate(ops):
                if not isinstance(op, dict) or op.get("op") not in CONFIG_OPS:
                    res.errors.append(f"op[{i}]: unknown operation; use one of {sorted(CONFIG_OPS)}")
                    continue
                kind = op["op"]
                if kind == "set_setting":
                    if "value" not in op or op["value"] is None:
                        res.errors.append(f"op[{i}] set_setting needs a value (use reset_setting to go back to the default)")
                    else:
                        _setting(res, actor, op.get("key"), op["value"])
                elif kind == "reset_setting":
                    _setting(res, actor, op.get("key"), None)
                elif kind == "update_rule":
                    _update_rule(res, actor, op)
                elif kind == "create_rule":
                    _create_rule(res, actor, op)
                else:
                    _set_role(res, actor, op)
            if res.errors or dry_run:
                raise _Rollback
    except _Rollback:
        pass
    finally:
        invalidate_overrides()  # a rolled-back dry run must not leave its values in the cache
    res.reverse.reverse()
    if not res.errors and not res.diff:
        res.errors.append("nothing would change")
    return res
