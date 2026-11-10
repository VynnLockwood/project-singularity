import datetime
import re
import decimal
import uuid


def jsonable(v):
    """Make a database value safe to put in JSON (and to feed back in as an operation value)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return str(v)


_PG_TO_ALLOWED = {
    "text": "text", "integer": "integer", "bigint": "bigint", "smallint": "smallint",
    "boolean": "boolean", "date": "date", "uuid": "uuid", "jsonb": "jsonb", "real": "real",
    "double precision": "double precision", "numeric": "numeric",
    "timestamp with time zone": "timestamptz", "timestamp without time zone": "timestamp",
}


def pg_type_to_allowed(t: str, enums=(), schema: str | None = None) -> str | None:
    """Map a Postgres type name (format_type output) to a type our operations accept.

    `enums` are the enum type names of the schema; an enum maps to its own name (with or without the schema prefix).
    """
    t = t.strip().lower()
    bare = t.removeprefix(f"{schema}.") if schema else t
    if bare in enums:
        return bare
    if t in _PG_TO_ALLOWED:
        return _PG_TO_ALLOWED[t]
    if t.startswith("character varying(") and t.endswith(")"):
        return "varchar(" + t[len("character varying("):-1] + ")"
    if t.startswith("numeric(") and t.endswith(")"):
        return t.replace(" ", "")
    return None


_DEFAULT_FUNCS = {"now()": "now()", "gen_random_uuid()": "gen_random_uuid()", "current_date": "current_date"}
_STR_DEFAULT = re.compile(r"^'((?:[^']|'')*)'(?:::[a-z ]+(?:\(\d+(?:,\d+)?\))?)?$", re.IGNORECASE)


def pg_default_to_allowed(expr: str | None, pg_type: str) -> tuple[bool, object]:
    """Map a column's database default expression back to a default our operations accept.

    Returns (True, value) when it maps (value None = no default), (False, None) when it cannot be restored.
    """
    if expr is None:
        return True, None
    e = expr.strip()
    if e.lower() in _DEFAULT_FUNCS:
        return True, _DEFAULT_FUNCS[e.lower()]
    if e.lower() in ("true", "false"):
        return True, e.lower() == "true"
    if re.fullmatch(r"-?\d+", e):
        return True, int(e)
    if re.fullmatch(r"-?\d+\.\d+", e):
        return True, float(e)
    m = _STR_DEFAULT.match(e)
    if m:
        return True, m.group(1).replace("''", "'")
    return False, None
