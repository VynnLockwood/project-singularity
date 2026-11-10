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


def _validate_move(o: dict, managed: dict[str, int], states: dict[str, dict] | None) -> list[str]:
    t, src, dst = o["table"], o["from_schema"], o["to_schema"]
    if src == dst:
        return ["from_schema and to_schema are the same schema"]
    missing = [s for s in (src, dst) if s not in managed]
    if missing:
        return [f"schema {s!r} does not exist or is not managed" for s in missing]
    if not states or src not in states or dst not in states:
        return ["schema state unavailable: cannot check move_table"]
    from .state import enums_of, views_of
    a, b = states[src], states[dst]
    if t not in a:
        return [f"table {t!r} does not exist in schema {src!r}"]
    errors = []
    if t in b or t in views_of(b) or t in enums_of(b):
        errors.append(f"schema {dst!r} already has a table, view or enum named {t!r}")
    deps = sorted(v for v, d in views_of(a).items() if t in d["tables"])
    if deps:
        errors.append(f"view(s) {', '.join(deps)} in {src!r} depend on {t!r}; drop or recreate them first")
    typed = sorted({c["type"] for c in a[t]["columns"].values() if c["type"] in enums_of(a)})
    if typed:
        errors.append(f"table {t!r} has columns of enum type {', '.join(typed)}; a type cannot cross schemas, "
                      "so change those columns to another type first")
    links = sorted(a[t].get("links", set()))
    if links:
        errors.append(f"table {t!r} has foreign keys to or from {', '.join(links)}; "
                      "a foreign key cannot cross schemas, so drop it first")
    return errors


def _validate_database_ops(ops: list[dict], managed: dict[str, int], max_schemas: int,
                           states: dict[str, dict] | None = None) -> list[str]:
    """Validate schema-level ops against the managed schemas (name -> table count)."""
    errors: list[str] = []
    for o in ops:
        kind = o["op"]
        if kind == "move_table":
            errors += _validate_move(o, managed, states)
        if kind == "create_schema":
            if o["name"] in managed:
                errors.append(f"schema {o['name']!r} already exists")
            elif len(managed) >= max_schemas:
                errors.append(
                    f"cannot create schema {o['name']!r}: {len(managed)} managed schemas already exist "
                    f"(limit limits.maxSchemas is {max_schemas})")
        elif kind == "rename_schema":
            if o["schema"] == "master":
                errors.append("the master schema cannot be renamed")
            elif o["schema"] not in managed:
                errors.append(f"schema {o['schema']!r} does not exist or is not managed")
            if o["new_name"] == o["schema"]:
                errors.append("new_name is the same as the current name")
            elif o["new_name"] in managed:
                errors.append(f"schema {o['new_name']!r} already exists")
        elif kind == "drop_schema":
            if o["schema"] == "master":
                errors.append("the master schema cannot be dropped")
            elif o["schema"] not in managed:
                errors.append(f"schema {o['schema']!r} does not exist or is not managed")
            elif managed[o["schema"]] > 0:
                errors.append(
                    f"schema {o['schema']!r} still has {managed[o['schema']]} table(s); "
                    "only empty schemas can be dropped")
    return errors


def run(raw_ops, schema_state: dict, rule_list: list[dict], *, actor: str | None = None,
        max_ops: int = 50, skip_rules: bool = False, allow_bulk: bool = False,
        max_script_rows: int | None = None, schema: str = "master",
        managed_schemas: dict[str, int] | None = None, max_schemas: int = 20,
        schema_states: dict[str, dict] | None = None) -> CheckResult:
    """Check `raw_ops` against the current schema state and rules.

    skip_rules is used only for reverts, which must be able to restore an old
    state even if today's rules would not allow it. Shape and semantic checks
    still apply.

    bulk_insert is produced by script intake only: it is refused unless the caller
    passes allow_bulk=True, and then expected_rows is capped by limits.maxScriptRows
    (max_script_rows, read from the config when not given).
    """
    bulk = [o for o in raw_ops if isinstance(o, dict) and o.get("op") == "bulk_insert"] if isinstance(raw_ops, list) else []
    if bulk and not allow_bulk:
        return CheckResult(ok=False, operations=raw_ops, errors=[
            "bulk_insert cannot be proposed directly: it is produced by script intake only. Use insert_rows for small data."])
    try:
        ops = operations.parse_ops(raw_ops, max_ops=max_ops)
    except operations.OpError as exc:
        return CheckResult(ok=False, operations=raw_ops if isinstance(raw_ops, list) else [], errors=exc.errors)

    pre_errors: list[str] = []
    if bulk:
        if max_script_rows is None:
            from config.siconfig import get_config
            max_script_rows = get_config()["limits"]["maxScriptRows"]
        for o in ops:
            if o["op"] == "bulk_insert" and o["expected_rows"] > max_script_rows:
                pre_errors.append(
                    f"bulk_insert into {o['table']}: {o['expected_rows']} rows exceeds the script row limit ({max_script_rows})")

    if skip_rules:
        ops_out, violations, auto = ops, [], []
    else:
        ops_out, violations, auto = rules.evaluate(ops, rule_list)

    conv = Conventions.from_rules(rule_list)
    if any(o["op"] in operations.DATABASE_OPS for o in ops_out):
        if managed_schemas is None:
            errors = ["managed schema state unavailable: cannot check schema operations"]
        else:
            errors = _validate_database_ops(ops_out, managed_schemas, max_schemas, schema_states)
    else:
        errors, _ = state_mod.validate(ops_out, schema_state, conv)
    errors = pre_errors + errors
    res = CheckResult(ok=False, operations=ops_out, errors=errors, violations=violations, auto_added=auto)
    if not errors:
        try:
            res.sql = sqlgen.render(ops_out, schema_state, actor, conv, schema)
            res.diff = [sqlgen.describe(o) for o in ops_out]
        except sqlgen.UnsafeOp as exc:
            res.errors.append(str(exc))
    res.ok = not res.blocked
    return res
