"""Write sources as a pg_dump-style script that scripts.sqlparse / scripts.copyparse read.

Plan shape (all keys optional):
  {"mapping": {"tables": {"<source>": {"target": "<table>", ...}}},   # the scripts mapping
   "filters": {"<source>": [{"column", "op": equals|in|not_empty, "value"}]},  # ANDed
   "skip":    {"<source>": [1-based data-row indexes]},
   "existing": ["<table>", ...]}   # tables known to exist; default: introspected from `schema`
The script names tables by source name (the mapping renames them to the target at load time).
"""
from .profile import convert_value

_PG_TYPES = {"text": "text", "integer": "bigint", "numeric": "numeric", "boolean": "boolean", "date": "date"}
_OPS = ("equals", "in", "not_empty")


def _ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def _copy_escape(value):
    if value is None:
        return "\\N"
    return (str(value).replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def _cell(row, i):
    return row[i] if i < len(row) else None


def _text(v):
    return "" if v is None else str(v).strip()


def apply_filter(rows, headers, filter):
    """Indexes (0-based) of the rows that pass every condition of `filter` (a list, ANDed)."""
    checks = []
    for cond in filter or []:
        if cond.get("column") not in headers:
            raise ValueError(f"filter column {cond.get('column')!r} not in headers")
        if cond.get("op") not in _OPS:
            raise ValueError(f"filter op {cond.get('op')!r} must be one of {_OPS}")
        checks.append((headers.index(cond["column"]), cond["op"], cond.get("value")))
    keep = []
    for n, row in enumerate(rows):
        ok = True
        for i, op, value in checks:
            cell = _text(_cell(row, i))
            if op == "equals":
                ok = cell == _text(value)
            elif op == "in":
                ok = cell in {_text(v) for v in value or []}
            else:
                ok = cell != ""
            if not ok:
                break
        if ok:
            keep.append(n)
    return keep


def _existing(plan, schema):
    if plan.get("existing") is not None:
        return set(plan["existing"])
    from schema import introspect
    return set(introspect.snapshot(schema))


def _column_type(column, values):
    t = _PG_TYPES.get(column["type"], "text")
    if t == "date" and any(len(v) > 10 for v in values if v is not None):
        return "timestamp"
    return t


def write(sources, profiles, plan, schema):
    plan = plan or {}
    existing = None
    parts = []
    for source, prof in zip(sources, profiles):
        name = source["name"]
        headers = source["headers"]
        cols = prof["columns"]
        rows = source["rows"]
        keep = set(apply_filter(rows, headers, (plan.get("filters") or {}).get(name)))
        skip = set((plan.get("skip") or {}).get(name) or [])
        out = []
        for n, row in enumerate(rows):
            if n in keep and (n + 1) not in skip:
                out.append([convert_value(_cell(row, i), cols[i]) for i in range(len(headers))])
        target = ((plan.get("mapping") or {}).get("tables") or {}).get(name, {}).get("target", name)
        if existing is None:
            existing = _existing(plan, schema)
        col_list = ", ".join(_ident(h) for h in headers)
        if target not in existing:
            defs = ",\n".join(
                f"    {_ident(h)} {_column_type(cols[i], [r[i] for r in out])}" for i, h in enumerate(headers))
            parts.append(f"CREATE TABLE {_ident(name)} (\n{defs}\n);\n\n")
        body = "".join("\t".join(_copy_escape(v) for v in r) + "\n" for r in out)
        parts.append(f"COPY {_ident(name)} ({col_list}) FROM stdin;\n{body}\\.\n\n")
    return "".join(parts).encode("utf-8")
