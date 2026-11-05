import datetime
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


def pg_type_to_allowed(t: str) -> str | None:
    """Map a Postgres type name (format_type output) to a type our operations accept."""
    t = t.strip().lower()
    if t in _PG_TO_ALLOWED:
        return _PG_TO_ALLOWED[t]
    if t.startswith("character varying(") and t.endswith(")"):
        return "varchar(" + t[len("character varying("):-1] + ")"
    if t.startswith("numeric(") and t.endswith(")"):
        return t.replace(" ", "")
    return None
