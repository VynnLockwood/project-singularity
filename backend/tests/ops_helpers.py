"""Shared helpers for the Plan B operation tests (tests/test_ops_*.py)."""
from gate import check
from gate.defaults import DEFAULT_RULES
from schema import executor, introspect, query

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
ME = "me@example.com"


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


def apply(ops, *, skip_rules=True, max_rows=1000, schema="master", dry_run=False):
    """Check against the real snapshot, then execute. Rules are skipped by default: these tests are about the ops."""
    st = introspect.snapshot(schema)
    res = check.run(ops, st, RULES, actor=ME, skip_rules=skip_rules, schema=schema)
    assert res.ok, (res.errors, res.violations)
    return executor.execute(res.operations, st, ME, max_rows=max_rows, rules=RULES, schema=schema, dry_run=dry_run)


def gate(ops, schema="master", skip_rules=True):
    """The gate's verdict only (no execution)."""
    return check.run(ops, introspect.snapshot(schema), RULES, actor=ME, skip_rules=skip_rules, schema=schema)


def make_table(name="t", extra=None, rows=None):
    """A plain table: pk <name>_id plus `extra` columns; optional rows."""
    cols = [col(f"{name}_id", "integer", primary_key=True, identity=True), *(extra or [col("a")])]
    apply([{"op": "create_table", "name": name, "columns": cols}])
    if rows:
        apply([{"op": "insert_rows", "table": name, "rows": rows}])


def rows(sql):
    return query.run_select(sql)["rows"]


def gate_db(ops, skip_rules=True):
    """The gate's verdict on database-level ops (schemas, move_table): needs the managed schemas and their states."""
    managed = {m["name"]: m["tables"] for m in introspect.managed_schemas()}
    return check.run(ops, introspect.snapshot("master"), RULES, actor=ME, skip_rules=skip_rules, managed_schemas=managed,
                     max_schemas=20, schema_states={s: introspect.snapshot(s) for s in managed})
