"""Validate an uploaded script: parse, map, gate, orphan check and a rolled-back dry run (spec 2.6).

validate(script, mapping) never changes any managed schema: the dry run runs the generated operations through
schema.executor with dry_run=True (one transaction, always rolled back, sequences restored).

Pipeline (each step stops the pipeline when it finds errors, except that the gate and the orphan
check both run so the report shows both):
  read (checksum) -> parse_script -> apply_mapping (mapping None: suggest_mapping) -> bind_script
  -> gate.check.run(allow_bulk=True) -> orphan check in Python -> dry run (only when all clean)

Report (JSON-serializable, bounded; saved on script.report):
  {"ok": bool, "script_id": int, "statements": int,
   "mapping_used": "given" | "suggested" | None,              # None: stopped before mapping
   "errors": [{"kind": "storage"|"parse"|"mapping"|"rows", "line", "statement", "message"}],
   "errors_truncated": int,                                   # errors beyond MAX_ERRORS
   "ignored": [{"line", "statement"}], "ignored_truncated": int,
   "skipped": [{"line", "statement", "reason", "kind"}], "skipped_truncated": int,
   "ops_summary": {"total": int, "by_op": {op: count}, "tables": [table, ...]},
   "gate": {"errors": [str], "blocked": [violation], "warnings": [violation], "truncated": int},
   "tables": [{"name", "source", "rows", "orphans", "sample_orphans": [
                {"line", "row", "column", "value", "parent": "<table>.<key>"}]}],
   "dry_run": {"ok": bool, "skipped": bool, "error": str | None, "rows_loaded": {table: rows}}}

ok = no errors, no gate errors or blocking violations, no orphans, and the dry run succeeded.
scripts that are "submitted" or "expired" are refused. script.status becomes "validated" only when ok;
a failing re-validation never downgrades a validated script. The mapping used is stored on script.mapping.

Orphans: a child row whose ref key (non-NULL, non-empty) is not among the parent's keys. Parent keys
come from the parent's rows in the script plus, when the parent table already exists in the target schema, its
current rows. Integer / numeric keys are compared as numbers (like the executor's cast); others as text.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation

from psycopg import sql

from config.siconfig import get_config
from gate import check, sqlgen
from schema import executor, introspect
from schema.conn import ro_connection
from settings_app import services as rule_service

from . import storage
from .copyparse import RowStreamError
from .mapping import apply_mapping, bind_script, suggest_mapping
from .models import Script
from .rowsource import make_row_source
from .sqlparse import parse_script

MAX_ERRORS = 200
MAX_IGNORED = 100
MAX_VIOLATIONS = 200
MAX_SAMPLES = 10
MAX_MESSAGE = 2000
INT_TYPES = {"smallint", "integer", "bigint"}
CHAR_TYPES = {"char", "character", "bpchar"}  # char(n): Postgres strips trailing blanks to text


def _clip(s, n: int = MAX_MESSAGE) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def _new_report(script: Script) -> dict:
    return {
        "ok": False, "script_id": script.pk, "statements": 0, "mapping_used": None,
        "errors": [], "errors_truncated": 0, "ignored": [], "ignored_truncated": 0,
        "skipped": [], "skipped_truncated": 0,
        "ops_summary": {"total": 0, "by_op": {}, "tables": []},
        "gate": {"errors": [], "blocked": [], "warnings": [], "truncated": 0},
        "tables": [],
        "dry_run": {"ok": False, "skipped": True, "error": None, "rows_loaded": {}},
    }


def _add_error(report: dict, kind: str, line: int, statement: str, message: str) -> None:
    if len(report["errors"]) >= MAX_ERRORS:
        report["errors_truncated"] += 1
        return
    report["errors"].append({"kind": kind, "line": line, "statement": _clip(statement, 200),
                             "message": _clip(message)})


def _ignored(report: dict, entries: list[str]) -> None:
    for e in entries:
        if len(report["ignored"]) >= MAX_IGNORED:
            report["ignored_truncated"] += 1
            continue
        head, _, rest = e.partition(": ")
        try:
            line = int(head.removeprefix("line "))
        except ValueError:
            line, rest = 0, e
        report["ignored"].append({"line": line, "statement": rest})


def _summary(ops: list[dict]) -> dict:
    from gate.operations import affected_tables
    try:
        names = sorted(set(affected_tables(ops)))
    except (KeyError, TypeError):
        names = []
    return {"total": len(ops), "by_op": dict(Counter(o.get("op") for o in ops)), "tables": names}


# ---------------------------------------------------------------- orphans

def _normalizer(type_: str | None):
    base = (type_ or "").split("(")[0].strip().lower()
    if base in INT_TYPES:
        def norm(v):
            try:
                return int(v.strip())
            except ValueError:
                return v
        return norm
    if base == "numeric":
        def norm(v):
            try:
                return Decimal(v.strip()).normalize()
            except InvalidOperation:
                return v
        return norm
    if base in CHAR_TYPES:  # char(n) semantics: trailing blanks are not significant
        return lambda v: str(v).rstrip()
    if base == "uuid":  # uuid input is case-insensitive and ignores surrounding blanks
        return lambda v: str(v).strip().lower()
    return lambda v: v


def _key_type(ops: list[dict], state: dict, table: str, column: str) -> str | None:
    for o in ops:
        if o.get("op") == "create_table" and o.get("name") == table:
            for c in o.get("columns", []):
                if c.get("name") == column:
                    return c.get("type")
    return (state.get(table, {}).get("columns", {}).get(column) or {}).get("type")


def _parent_keys(parent: str, key: str, ops, bulks, parsed, state, norm, schema: str = "master") -> set:
    keys: set = set()
    if key in state.get(parent, {}).get("columns", {}):
        with ro_connection() as conn:
            # a ref may name its parent as the SQL does ("crm.district"); it is read from the script's own schema
            q = sql.SQL("SELECT {}::text AS k FROM {} WHERE {} IS NOT NULL").format(
                sql.Identifier(key), sqlgen.tbl(parent.rsplit(".", 1)[-1], schema), sql.Identifier(key))
            for r in conn.execute(q):
                keys.add(norm(r["k"]))
    for b in bulks:
        if b["table"] == parent and key in b["columns"]:
            stream = parsed.streams[b["source"]["stream"]]
            i = stream.columns.index(b["source"]["columns"][b["columns"].index(key)])
            for row in stream.rows():
                v = row[i]
                if v is not None and v != "":
                    keys.add(norm(v))
    return keys


def _orphans(report: dict, ops: list[dict], parsed, state: dict, schema: str = "master") -> None:
    bulks = [o for o in ops if o.get("op") == "bulk_insert"]
    cache: dict[tuple[str, str], set] = {}
    for b in bulks:
        entry = {"name": b["table"], "source": b["source"]["stream"], "rows": b["expected_rows"],
                 "orphans": 0, "sample_orphans": []}
        report["tables"].append(entry)
        stream = parsed.streams[b["source"]["stream"]]
        checks = []
        for r in b.get("refs", []):
            col = b["source"]["columns"][b["columns"].index(r["source_column"])]
            norm = _normalizer(_key_type(ops, state, r["parent_table"], r["parent_key"]))
            ck = (r["parent_table"], r["parent_key"])
            if ck not in cache:
                cache[ck] = _parent_keys(*ck, ops, bulks, parsed, state, norm, schema)
            checks.append((stream.columns.index(col), col, cache[ck], norm, f"{ck[0]}.{ck[1]}"))
        if not checks:
            continue
        for n, (line, row) in enumerate(stream.numbered_rows(), 1):
            for i, col, keys, norm, parent in checks:
                v = row[i]
                if v is None or v == "" or norm(v) in keys:
                    continue
                entry["orphans"] += 1
                if len(entry["sample_orphans"]) < MAX_SAMPLES:
                    entry["sample_orphans"].append(
                        {"line": line, "row": n, "column": col, "value": _clip(v, 200), "parent": parent})


# ---------------------------------------------------------------- main

def validate(script: Script, mapping: dict | None, schema: str = "master") -> dict:
    introspect.require_managed(schema)
    report = _new_report(script)
    report["schema"] = schema
    kept = (script.mapping, (script.report or {}).get("validated_schema", "master"))
    if script.status in (Script.Status.SUBMITTED, Script.Status.EXPIRED):
        _add_error(report, "storage", 0, "",
                   f"script #{script.pk} is {script.status}; only uploaded or validated scripts can be validated")
        report["ok"] = False
        _save(script, report, kept)
        return report
    try:
        _run(report, script, mapping, schema)
    except Exception as exc:  # last resort: never crash, report a failed run instead
        _add_error(report, "rows", 0, "", f"unexpected error: {_clip(exc)}")
        report["dry_run"] = {"ok": False, "skipped": report["dry_run"]["skipped"],
                             "error": f"unexpected: {type(exc).__name__}: {exc}", "rows_loaded": {}}
    report["ok"] = (not report["errors"] and not report["errors_truncated"] and not report["gate"]["errors"]
                    and not report["gate"]["blocked"] and all(t["orphans"] == 0 for t in report["tables"])
                    and report["dry_run"]["ok"])
    _save(script, report, kept)
    return report


def _save(script: Script, report: dict, kept: tuple) -> None:
    fields = ["report", "mapping"]
    if report["ok"]:
        report["validated_schema"] = report["schema"]
    elif script.status == Script.Status.VALIDATED:
        # a failed run never replaces what passed: keep the validated mapping and schema for submit
        script.mapping, report["validated_schema"] = kept
    script.report = report
    # validated -> uploaded on failure was reverted by review: never downgrade a validated script
    if script.status == Script.Status.UPLOADED or (script.status == Script.Status.VALIDATED and report["ok"]):
        script.status = Script.Status.VALIDATED if report["ok"] else Script.Status.UPLOADED
        fields.append("status")
    script.save(update_fields=fields)


def _run(report: dict, script: Script, mapping: dict | None, schema: str = "master") -> None:
    if mapping is not None:
        script.mapping = mapping
        report["mapping_used"] = "given"
    try:
        text = storage.read_text(script)
    except storage.ScriptError as exc:
        _add_error(report, "storage", 0, "", str(exc))
        return

    parsed = parse_script(text)
    report["statements"] = parsed.statements
    _ignored(report, parsed.ignored)
    report["skipped"] = parsed.skipped[:MAX_IGNORED]
    report["skipped_truncated"] = max(0, len(parsed.skipped) - MAX_IGNORED)
    if not parsed.errors and not parsed.ops and not parsed.streams:
        _add_error(report, "parse", 0, "", "Nothing to import: the file has no tables or rows.")
        return
    if parsed.errors:
        for e in parsed.errors:
            _add_error(report, "parse", e.line, e.statement, e.message)
        return

    state = introspect.snapshot(schema)
    external = introspect.external_state(state, schema)  # tables the script does not create (external refs)
    if mapping is None:
        mapping = suggest_mapping(parsed)
        script.mapping = mapping
        report["mapping_used"] = "suggested"
    mapped = apply_mapping(parsed, mapping, external)
    if mapped.issues:
        for i in mapped.issues:
            # line 0 = a mapping problem (statement is the mapping path); otherwise a bad data row
            _add_error(report, "mapping" if i.line == 0 else "rows", i.line, i.statement, i.message)
        return
    ops = bind_script(mapped.ops, script.pk)
    report["ops_summary"] = _summary(ops)

    limits = get_config()["limits"]
    max_ops = limits["maxOpsPerScript"]
    if len(ops) > max_ops:
        _add_error(report, "rows", 0, "",
                   f"script has {len(ops)} operations, above the limit maxOpsPerScript ({max_ops})")
        return
    rules = rule_service.active_rules()
    res = check.run(ops, state, rules, actor=script.owner.email, max_ops=max_ops,
                    allow_bulk=True, schema=schema)
    gate = report["gate"]
    for e in res.errors:
        _gate_add(gate, "errors", _clip(e))
    for v in res.violations:
        _gate_add(gate, "blocked" if v["severity"] == "block" else "warnings", v)

    try:
        # external parents are in `external`, keyed "<schema>.<table>": merge so their live rows count
        _orphans(report, ops, parsed, {**state, **external}, schema)
    except RowStreamError as exc:  # apply_mapping already read every stream, so this is unexpected
        _add_error(report, "rows", exc.issue.line, exc.issue.statement, exc.issue.message)
        return

    if res.blocked or any(t["orphans"] for t in report["tables"]):
        return
    dry = report["dry_run"]
    dry["skipped"] = False
    try:
        executor.execute(res.operations, state, script.owner.email, max_rows=limits["maxRowsPerDataOp"],
                         rules=rules, row_source=make_row_source(script, parsed=parsed), dry_run=True,
                         schema=schema)
    except executor.ApplyError as exc:
        dry["error"] = _clip(exc)
        return
    dry["ok"] = True
    dry["rows_loaded"] = {o["table"]: o["expected_rows"] for o in res.operations if o["op"] == "bulk_insert"}


def _gate_add(gate: dict, key: str, item) -> None:
    if len(gate["errors"]) + len(gate["blocked"]) + len(gate["warnings"]) >= MAX_VIOLATIONS:
        gate["truncated"] += 1
        return
    gate[key].append(item)
