"""One call that runs the whole gate: shape, semantics, rules, SQL, diff."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import operations, rules, sqlgen, state as state_mod
from .conventions import Conventions


@dataclass
class CheckResult:
    ok: bool                       # no errors and no `block` violations
    operations: list[dict]         # normalized (auto-added columns inserted)
    errors: list[str] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    auto_added: list[str] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)
    diff: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.errors) or any(v["severity"] == "block" for v in self.violations)

    def as_dict(self) -> dict:
        return {
            "errors": self.errors,
            "violations": self.violations,
            "autoAdded": self.auto_added,
        }


def run(raw_ops, schema_state: dict, rule_list: list[dict], *, actor: str | None = None,
        max_ops: int = 50, skip_rules: bool = False) -> CheckResult:
    """Check `raw_ops` against the current schema state and rules.

    skip_rules is used only for reverts, which must be able to restore an old
    state even if today's rules would not allow it. Shape and semantic checks
    still apply.
    """
    try:
        ops = operations.parse_ops(raw_ops, max_ops=max_ops)
    except operations.OpError as exc:
        return CheckResult(ok=False, operations=raw_ops if isinstance(raw_ops, list) else [], errors=exc.errors)

    if skip_rules:
        ops_out, violations, auto = ops, [], []
    else:
        ops_out, violations, auto = rules.evaluate(ops, rule_list)

    conv = Conventions.from_rules(rule_list)
    errors, _ = state_mod.validate(ops_out, schema_state, conv)
    res = CheckResult(ok=False, operations=ops_out, errors=errors, violations=violations, auto_added=auto)
    if not errors:
        res.sql = sqlgen.render(ops_out, schema_state, actor, conv)
        res.diff = [sqlgen.describe(o) for o in ops_out]
    res.ok = not res.blocked
    return res
