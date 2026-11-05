import hashlib

from psycopg import sql

from approvals import service as changes
from approvals.models import Change
from config.siconfig import get_config
from schema import introspect, rows as rows_mod
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


def _describe(table: str) -> dict:
    d = introspect.describe_table(table)
    if d is None:
        raise DuplicateError("table not found", 404)
    return d


def _text(c: dict) -> bool:
    return c["type"] == "text" or c["type"].startswith("character varying")


def default_fields(columns: dict) -> list[str]:
    names = [n for n, c in columns.items() if n not in rows_mod.AUDIT and _text(c)]
    named = [n for n in names if "name" in n.lower()]
    if named:
        return named
    label = rows_mod.label_column(columns)
    return [label] if label else []


def group_key(table: str, ids: list[int]) -> str:
    return hashlib.sha256(f"{table}:{','.join(map(str, sorted(ids)))}".encode()).hexdigest()


def scan(table: str, user, *, fields: list[str] | None = None, threshold: float | None = None) -> dict:
    d = _describe(table)
    columns = {c["name"]: c for c in d["columns"]}
    if "id" not in columns:
        raise DuplicateError("this table has no id column")
    fields = fields or default_fields(columns)
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

    q = sql.SQL("SELECT id, {cols} FROM {t}{w} ORDER BY id LIMIT {n}").format(
        cols=sql.SQL(", ").join(sql.Identifier(f) for f in fields), t=sql.Identifier("master", table),
        w=sql.SQL(' WHERE "isActive" IS DISTINCT FROM false') if "isActive" in columns else sql.SQL(""),
        n=sql.Literal(max_rows + 1))
    with ro_connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        data = conn.execute(q).fetchall()
    truncated = len(data) > max_rows
    data = data[:max_rows]

    found = _engine().find_duplicates([(r["id"], [r[f] or "" for f in fields]) for r in data], float(threshold))

    existing = {g.key: g for g in DuplicateGroup.objects.filter(table_name=table)}
    keep, created = set(), 0
    for ids, score in found:
        key = group_key(table, ids)
        keep.add(key)
        g = existing.get(key)
        if g is None:
            DuplicateGroup.objects.create(table_name=table, fields=fields, key=key, member_ids=ids, score=score)
            created += 1
        else:
            g.score, g.fields = score, fields
            g.save(update_fields=["score", "fields"])
    stale = [g.pk for k, g in existing.items() if k not in keep]
    DuplicateGroup.objects.filter(pk__in=stale).delete()
    return {"table": table, "scanned": len(data), "truncated": truncated, "groups": len(found),
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
    return {"id": g.pk, "table": g.table_name, "fields": g.fields, "memberIds": g.member_ids,
            "score": round(g.score, 3), "status": state(g), "detectedAt": g.detected_at.isoformat(),
            "mergeChange": g.merge_change_id}


def detail(g: DuplicateGroup) -> dict:
    q = sql.SQL("SELECT * FROM {t} WHERE id = ANY(%s) ORDER BY id").format(t=sql.Identifier("master", g.table_name))
    with ro_connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        members = [{k: jsonable(v) for k, v in r.items()} for r in conn.execute(q, (g.member_ids,)).fetchall()]
    refs = [r for r in introspect.graph()["relations"] if r["to"]["table"] == g.table_name]
    return {**summary(g), "members": members,
            "referencedBy": [{"table": r["from"]["table"], "column": r["from"]["column"]} for r in refs]}


def merge_operations(g: DuplicateGroup, survivor_id: int) -> list[dict]:
    if survivor_id not in g.member_ids:
        raise DuplicateError("the survivor must be one of the group's records")
    losers = [i for i in g.member_ids if i != survivor_id]
    ops = []
    for r in introspect.graph()["relations"]:
        if r["to"]["table"] == g.table_name and r["to"]["column"] == "id":
            ops.append({"op": "update_rows", "table": r["from"]["table"],
                        "where": {r["from"]["column"]: losers if len(losers) > 1 else losers[0]},
                        "set": {r["from"]["column"]: survivor_id}})
    ops.append({"op": "deactivate_rows", "table": g.table_name, "where": {"id": losers if len(losers) > 1 else losers[0]}})
    return ops


def propose_merge(g: DuplicateGroup, survivor_id: int, user) -> Change:
    if state(g) in ("merged", "merge_pending"):
        raise DuplicateError("this group already has a merge in progress", 409)
    ops = merge_operations(g, survivor_id)
    try:
        change = changes.propose(user, ops, source="web",
                                 prompt=f"Merge duplicates in {g.table_name}: keep #{survivor_id}")
    except changes.ChangeError as exc:
        raise DuplicateError(exc.message, 403 if exc.code == "forbidden" else 400) from exc
    g.merge_change = change
    g.decided_by = user
    g.save(update_fields=["merge_change", "decided_by"])
    return change
