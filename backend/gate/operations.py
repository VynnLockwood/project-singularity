"""Structured operations: the only way anything changes `master` (spec section 2).

The AI never sends write SQL. It sends a list of operations (plain dicts); this
module checks their *shape*. Semantic checks (does the table exist?) live in
`gate.state`, naming/type rules in `gate.rules`, and SQL in `gate.sqlgen`.
"""
from __future__ import annotations

import re

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Only these column types may be created. Anything else is rejected, so a type
# string can never smuggle SQL into a statement.
TYPE_RE = re.compile(
    r"^(text|integer|bigint|smallint|boolean|date|timestamptz|timestamp|uuid|jsonb|real|"
    r"double precision|numeric|numeric\(\d{1,2},\s?\d{1,2}\)|varchar\(\d{1,4}\))$"
)

# Defaults: JSON literals or one of these function calls. Nothing free-form.
DEFAULT_FUNCS = {"now()", "gen_random_uuid()", "current_date"}

SCHEMA_OPS = {
    "create_table", "add_column", "rename_table", "rename_column", "alter_column_type",
    "set_column_comment", "add_index", "add_foreign_key", "add_unique",
    "drop_column", "drop_table", "drop_index", "drop_constraint",
}
DATA_OPS = {"insert_rows", "update_rows", "deactivate_rows"}
ALL_OPS = SCHEMA_OPS | DATA_OPS
DESTRUCTIVE_OPS = {"drop_column", "drop_table", "drop_index", "drop_constraint"}

SCALAR = (str, int, float, bool, type(None))


class OpError(Exception):
    """One or more operations are malformed. `.errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def normalize_type(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower()).replace(", ", ",")


def _ident(v, what: str, errs: list[str], where: str) -> None:
    if not isinstance(v, str) or not IDENT_RE.match(v):
        errs.append(f"{where}: {what} must be an identifier (letters, digits, underscore; max 63 chars)")


def _check_column(col, errs: list[str], where: str, *, allow_references: bool = True) -> None:
    if not isinstance(col, dict):
        errs.append(f"{where}: column must be an object")
        return
    unknown = set(col) - {
        "name", "type", "nullable", "unique", "default", "comment", "references", "primary_key", "identity",
    }
    if unknown:
        errs.append(f"{where}: unknown column keys {sorted(unknown)}")
    _ident(col.get("name"), "column name", errs, where)
    t = col.get("type")
    if not isinstance(t, str) or not TYPE_RE.match(normalize_type(t)):
        errs.append(f"{where}: unsupported type {t!r}")
    for flag in ("nullable", "unique", "primary_key", "identity"):
        if flag in col and not isinstance(col[flag], bool):
            errs.append(f"{where}: {flag} must be true or false")
    if "default" in col:
        d = col["default"]
        if isinstance(d, str) and d in DEFAULT_FUNCS:
            pass
        elif not isinstance(d, SCALAR):
            errs.append(f"{where}: default must be a JSON scalar or one of {sorted(DEFAULT_FUNCS)}")
    if "comment" in col and not isinstance(col["comment"], str):
        errs.append(f"{where}: comment must be text")
    ref = col.get("references")
    if ref is not None:
        if not allow_references or not isinstance(ref, dict) or set(ref) - {"table", "column"}:
            errs.append(f"{where}: references must be {{table, column}}")
        else:
            _ident(ref.get("table"), "references.table", errs, where)
            _ident(ref.get("column", "id"), "references.column", errs, where)


def _check_where(w, errs: list[str], where: str) -> None:
    if not isinstance(w, dict) or not w:
        errs.append(f"{where}: where must be a non-empty object (updates without a filter are not allowed)")
        return
    for k, v in w.items():
        _ident(k, "where column", errs, where)
        if isinstance(v, list):
            if not v or not all(isinstance(x, SCALAR) and x is not None for x in v):
                errs.append(f"{where}: where.{k} list must contain non-null scalars")
        elif not isinstance(v, SCALAR) or v is None:
            errs.append(f"{where}: where.{k} must be a non-null scalar or a list of them")


def _check_values(vals, errs: list[str], where: str, what: str) -> None:
    if not isinstance(vals, dict) or not vals:
        errs.append(f"{where}: {what} must be a non-empty object")
        return
    for k, v in vals.items():
        _ident(k, f"{what} column", errs, where)
        if not isinstance(v, (*SCALAR, dict, list)):
            errs.append(f"{where}: {what}.{k} must be a JSON value")


def _check_cols_list(cols, errs: list[str], where: str) -> None:
    if not isinstance(cols, list) or not cols:
        errs.append(f"{where}: columns must be a non-empty list")
        return
    for c in cols:
        _ident(c, "column", errs, where)


def _reason(op, errs, where):
    r = op.get("reason")
    if not isinstance(r, str) or len(r.strip()) < 3:
        errs.append(f"{where}: destructive operations need a 'reason'")


def parse_ops(raw, *, max_ops: int = 50) -> list[dict]:
    """Validate shape of an operation list. Returns the list unchanged or raises OpError."""
    errs: list[str] = []
    if not isinstance(raw, list) or not raw:
        raise OpError(["operations must be a non-empty list"])
    if len(raw) > max_ops:
        raise OpError([f"too many operations ({len(raw)} > {max_ops})"])

    for i, op in enumerate(raw):
        where = f"op[{i}]"
        if not isinstance(op, dict):
            errs.append(f"{where}: must be an object")
            continue
        kind = op.get("op")
        if kind not in ALL_OPS:
            errs.append(f"{where}: unknown op {kind!r}")
            continue
        where = f"op[{i}] {kind}"
        allowed = {"op"}

        def keys(*names):
            allowed.update(names)

        if kind == "create_table":
            keys("name", "columns", "comment")
            _ident(op.get("name"), "table name", errs, where)
            cols = op.get("columns")
            if not isinstance(cols, list) or not cols:
                errs.append(f"{where}: columns must be a non-empty list")
            else:
                for c in cols:
                    _check_column(c, errs, where)
                names = [c.get("name") for c in cols if isinstance(c, dict)]
                if len(names) != len(set(names)):
                    errs.append(f"{where}: duplicate column names")
            if "comment" in op and not isinstance(op["comment"], str):
                errs.append(f"{where}: comment must be text")
        elif kind == "add_column":
            keys("table", "column")
            _ident(op.get("table"), "table", errs, where)
            _check_column(op.get("column"), errs, where)
        elif kind == "rename_table":
            keys("table", "new_name")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("new_name"), "new_name", errs, where)
        elif kind == "rename_column":
            keys("table", "column", "new_name")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            _ident(op.get("new_name"), "new_name", errs, where)
        elif kind == "alter_column_type":
            keys("table", "column", "type")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            if not isinstance(op.get("type"), str) or not TYPE_RE.match(normalize_type(op["type"])):
                errs.append(f"{where}: unsupported type {op.get('type')!r}")
        elif kind == "set_column_comment":
            keys("table", "column", "comment")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            if not isinstance(op.get("comment"), str):
                errs.append(f"{where}: comment must be text")
        elif kind in ("add_index", "add_unique"):
            keys("table", "columns", "name", *(["unique"] if kind == "add_index" else []))
            _ident(op.get("table"), "table", errs, where)
            _check_cols_list(op.get("columns"), errs, where)
            if op.get("name") is not None:
                _ident(op["name"], "name", errs, where)
            if "unique" in op and not isinstance(op["unique"], bool):
                errs.append(f"{where}: unique must be true or false")
        elif kind == "add_foreign_key":
            keys("table", "column", "ref_table", "ref_column", "name")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            _ident(op.get("ref_table"), "ref_table", errs, where)
            _ident(op.get("ref_column", "id"), "ref_column", errs, where)
            if op.get("name") is not None:
                _ident(op["name"], "name", errs, where)
        elif kind == "drop_column":
            keys("table", "column", "reason")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("column"), "column", errs, where)
            _reason(op, errs, where)
        elif kind == "drop_table":
            keys("table", "reason")
            _ident(op.get("table"), "table", errs, where)
            _reason(op, errs, where)
        elif kind == "drop_index":
            keys("name", "reason")
            _ident(op.get("name"), "name", errs, where)
            _reason(op, errs, where)
        elif kind == "drop_constraint":
            keys("table", "name", "reason")
            _ident(op.get("table"), "table", errs, where)
            _ident(op.get("name"), "name", errs, where)
            _reason(op, errs, where)
        elif kind == "insert_rows":
            keys("table", "rows")
            _ident(op.get("table"), "table", errs, where)
            rows = op.get("rows")
            if not isinstance(rows, list) or not rows:
                errs.append(f"{where}: rows must be a non-empty list")
            else:
                for r in rows:
                    _check_values(r, errs, where, "row")
        elif kind == "update_rows":
            keys("table", "where", "set")
            _ident(op.get("table"), "table", errs, where)
            _check_where(op.get("where"), errs, where)
            _check_values(op.get("set"), errs, where, "set")
        elif kind == "deactivate_rows":
            keys("table", "where")
            _ident(op.get("table"), "table", errs, where)
            _check_where(op.get("where"), errs, where)

        extra = set(op) - allowed
        if extra:
            errs.append(f"{where}: unknown keys {sorted(extra)}")

    if errs:
        raise OpError(errs)
    return raw


def kind_of(ops: list[dict]) -> str:
    """'schema' if any operation changes structure, else 'data'."""
    return "schema" if any(o["op"] in SCHEMA_OPS for o in ops) else "data"


def affected_tables(ops: list[dict]) -> list[str]:
    out: set[str] = set()
    for o in ops:
        if o["op"] == "create_table":
            out.add(o["name"])
        elif o["op"] == "rename_table":
            out.update((o["table"], o["new_name"]))
        elif o["op"] == "drop_index":
            continue  # index name only; table not known from the op
        else:
            out.add(o["table"])
        if o["op"] == "add_foreign_key":
            out.add(o["ref_table"])
        if o["op"] == "create_table":
            for c in o["columns"]:
                if c.get("references"):
                    out.add(c["references"]["table"])
        if o["op"] == "add_column" and o["column"].get("references"):
            out.add(o["column"]["references"]["table"])
    return sorted(out)
