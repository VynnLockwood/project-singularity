import hashlib

from psycopg import sql

from approvals import service as changes
from approvals.models import Change
from audit.models import record
from config.siconfig import get_config
from schema import introspect, roles, rows as rows_mod
from schema.conn import ro_connection
from schema.util import jsonable

from .models import DuplicateGroup


class DuplicateError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _engine():
    try:
        import singularity_engine
    except ImportError as exc:  # pragma: no cover - the Docker image and CI always build it
        raise DuplicateError("the duplicate-detection engine is not installed", 503) from exc
    return singularity_engine


def _describe(table: str, schema: str = "master") -> dict:
    try:
        d = introspect.describe_table(table, schema)
    except introspect.UnknownSchema as exc:
        raise DuplicateError(str(exc), 400) from exc
    if d is None:
        raise DuplicateError("table not found", 404)
    return d


def _text(c: dict) -> bool:
    return c["type"] == "text" or c["type"].startswith("character varying")


def default_fields(columns: dict, skip: set[str] = frozenset()) -> list[str]:
    names = [n for n, c in columns.items() if n not in skip and _text(c)]
    named = [n for n in names if "name" in n.lower()]
    if named:
        return named
    label = rows_mod.label_column(columns, skip)
    return [label] if label else []


def group_key(table: str, ids: list[int], schema: str = "master") -> str:
    prefix = table if schema == "master" else f"{schema}.{table}"  # master keys unchanged
    return hashlib.sha256(f"{prefix}:{','.join(map(str, sorted(ids)))}".encode()).hexdigest()


def audit_columns(described: dict) -> set[str]:
    """Columns that are bookkeeping, not data: every audit spelling plus this table's role columns (not the key)."""
    role_cols = roles.roles_of(described)
    return roles.hidden_columns() | {c for r, c in role_cols.items() if r != "primary_key"}


def scan(table: str, user, *, fields: list[str] | None = None, threshold: float | None = None,
         schema: str = "master") -> dict:
    d = _describe(table, schema)
    columns = {c["name"]: c for c in d["columns"]}
    role_cols = roles.roles_of(d)
    pk = role_cols.get("primary_key")
    if pk is None:
        raise DuplicateError("this table has no primary key column")
    fields = fields or default_fields(columns, set(role_cols.values()) | audit_columns(d))
    if not fields:
        raise DuplicateError("this table has no text column to compare")
    for f in fields:
        if f not in columns or not _text(columns[f]):
            raise DuplicateError(f"{f!r} is not a text column of {table}")
    cfg = get_config()["duplicates"]
    threshold = cfg["threshold"] if threshold is None else threshold
    if not isinstance(threshold, (int, float)) or not 0.5 <= threshold <= 1:
        raise DuplicateError("threshold must be between 0.5 and 1")
    max_rows = cfg["maxRows"]

    active = role_cols.get("is_active")
    q = sql.SQL("SELECT {pk} AS pk, {cols} FROM {t}{w} ORDER BY {pk} LIMIT {n}").format(
        pk=sql.Identifier(pk),
        cols=sql.SQL(", ").join(sql.Identifier(f) for f in fields), t=sql.Identifier(schema, table),
        w=sql.SQL(" WHERE {} IS DISTINCT FROM false").format(sql.Identifier(active)) if active else sql.SQL(""),
        n=sql.Literal(max_rows + 1))
    with ro_connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        data = conn.execute(q).fetchall()
    truncated = len(data) > max_rows
    data = data[:max_rows]

    found = _engine().find_duplicates([(r["pk"], [r[f] or "" for f in fields]) for r in data], float(threshold))

    existing = {g.key: g for g in DuplicateGroup.objects.filter(schema=schema, table_name=table)}
    keep, created = set(), 0
    for ids, score in found:
        key = group_key(table, ids, schema)
        keep.add(key)
        g = existing.get(key)
        if g is None:
            DuplicateGroup.objects.create(schema=schema, table_name=table, fields=fields, key=key, member_ids=ids, score=score)
            created += 1
        else:
            g.score, g.fields = score, fields
            g.save(update_fields=["score", "fields"])
    stale = [g.pk for k, g in existing.items() if k not in keep]
    DuplicateGroup.objects.filter(pk__in=stale).delete()
    return {"schema": schema, "table": table, "scanned": len(data), "truncated": truncated, "groups": len(found),
            "new": created, "removed": len(stale), "fields": fields}


def state(g: DuplicateGroup) -> str:
    c = g.merge_change
    if c is not None:
        if c.status == Change.Status.APPLIED:
            return "merged"
        if c.status == Change.Status.PENDING:
            return "merge_pending"
    return g.status


def summary(g: DuplicateGroup) -> dict:
    return {"id": g.pk, "schema": g.schema, "table": g.table_name, "fields": g.fields, "memberIds": g.member_ids,
            "score": round(g.score, 3), "status": state(g), "detectedAt": g.detected_at.isoformat(),
            "mergeChange": g.merge_change_id}


def detail(g: DuplicateGroup) -> dict:
    d = _describe(g.table_name, g.schema)
    pk = _table_pk(g.table_name, g.schema)
    present = {c["name"] for c in d["columns"]}
    drop = audit_columns(d) & present
    q = sql.SQL("SELECT * FROM {t} WHERE {pk} = ANY(%s) ORDER BY {pk}").format(
        t=sql.Identifier(g.schema, g.table_name), pk=sql.Identifier(pk))
    with ro_connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        members = [{k: jsonable(v) for k, v in r.items() if k not in drop} for r in conn.execute(q, (g.member_ids,)).fetchall()]
    refs = [r for r in introspect.graph(g.schema)["relations"] if r["to"]["table"] == g.table_name]
    return {**summary(g), "members": members, "primaryKey": pk, "hiddenColumns": sorted(drop),
            "referencedBy": [{"table": r["from"]["table"], "column": r["from"]["column"]} for r in refs]}


def _table_pk(table: str, schema: str = "master") -> str:
    d = _describe(table, schema)
    pk = roles.roles_of(d).get("primary_key")
    if pk is None:
        raise DuplicateError("this table has no primary key column")
    return pk


def merge_operations(g: DuplicateGroup, survivor_id: int) -> list[dict]:
    if survivor_id not in g.member_ids:
        raise DuplicateError("the survivor must be one of the group's records")
    losers = [i for i in g.member_ids if i != survivor_id]
    pk = _table_pk(g.table_name, g.schema)
    ops = []
    for r in introspect.graph(g.schema)["relations"]:
        if r["to"]["table"] == g.table_name and r["to"]["column"] == pk:
            ops.append({"op": "update_rows", "table": r["from"]["table"],
                        "where": {r["from"]["column"]: losers if len(losers) > 1 else losers[0]},
                        "set": {r["from"]["column"]: survivor_id}})
    ops.append({"op": "deactivate_rows", "table": g.table_name, "where": {pk: losers if len(losers) > 1 else losers[0]}})
    return ops


def propose_merge(g: DuplicateGroup, survivor_id: int, user, *, source: str = "web") -> Change:
    if state(g) in ("merged", "merge_pending"):
        raise DuplicateError("this group already has a merge in progress", 409)
    ops = merge_operations(g, survivor_id)
    try:
        change = changes.propose(user, ops, source=source,
                                 prompt=f"Merge duplicates in {g.table_name}: keep #{survivor_id}", schema=g.schema)
    except changes.ChangeError as exc:
        raise DuplicateError(exc.message, 403 if exc.code == "forbidden" else 400) from exc
    g.merge_change = change
    g.decided_by = user
    g.save(update_fields=["merge_change", "decided_by"])
    return change


def get_group(group_id) -> DuplicateGroup:
    g = None
    if isinstance(group_id, int) and not isinstance(group_id, bool):
        g = DuplicateGroup.objects.select_related("merge_change").filter(pk=group_id).first()
    if g is None:
        raise DuplicateError(f"duplicate group #{group_id} does not exist", 404)
    return g


def list_groups(*, schema: str | None = "master", table: str | None = None, status: str | None = None) -> list[dict]:
    """Group summaries. `schema=None` means every managed schema; `status` is the shown state (see `state`)."""
    qs = DuplicateGroup.objects.select_related("merge_change")
    if schema:
        qs = qs.filter(schema=introspect.require_managed(schema))
    if table:
        qs = qs.filter(table_name=table)
    items = [summary(g) for g in qs]
    return [g for g in items if g["status"] == status] if status else items


def dismiss(g: DuplicateGroup, user) -> dict:
    """Mark a group as not duplicates; a later scan will not bring it back."""
    g.status, g.decided_by = DuplicateGroup.Status.DISMISSED, user
    g.save(update_fields=["status", "decided_by"])
    record(user, "duplicates.dismissed", target=f"#{g.pk}", details={"table": g.table_name})
    return summary(g)
