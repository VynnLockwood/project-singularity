"""Staged loads: rows sent over MCP in chunks, stored as COPY text, finished through the script pipeline."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

from django.utils import timezone

from audit.models import record
from config.siconfig import get_config
from schema import introspect

from . import storage
from .models import StagedLoad

EDITOR = 1


class LoadError(Exception):
    pass


def _cfg() -> dict:
    cfg = get_config()
    return {**cfg["limits"], **cfg["loads"]}


def _limits(load: StagedLoad) -> dict:
    c = _cfg()
    return {"chunk_bytes": c["maxLoadChunkBytes"], "chunk_rows": c["maxLoadChunkRows"],
            "total_rows": c["maxScriptRows"], "total_bytes": c["maxLoadBytes"],
            "expires_at": load.expires_at.isoformat()}


def _full(load: StagedLoad) -> str:
    return os.path.join(storage.loads_dir(), load.path)


def _remove_file(load: StagedLoad) -> None:
    if load.path:
        try:
            os.unlink(_full(load))
        except FileNotFoundError:
            pass
        load.path = ""


def _mine(user, load_id) -> StagedLoad:
    """The load, or LoadError saying it does not exist (also for someone else's load)."""
    load = StagedLoad.objects.filter(pk=load_id).first() if isinstance(load_id, int) else None
    if load is None or (load.owner_id != user.pk and not user.can_approve):
        raise LoadError(f"load #{load_id} does not exist")
    return load


def begin(user, table: str, columns: list[str], schema: str = "master", refs: list[dict] | None = None) -> dict:
    if user is None or user.rank < EDITOR:
        raise PermissionError("Starting a load needs the editor role.")
    refs = refs or []
    introspect.require_managed(schema)
    state = introspect.snapshot(schema)
    if table not in state:
        raise LoadError(f"table {table!r} does not exist in {schema!r}. Call list_tables.")
    real = state[table]["columns"]
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
        raise LoadError("columns must be a non-empty list of column names")
    if len(set(columns)) != len(columns):
        raise LoadError("columns lists a name twice")
    carriers, ref_cols = set(), set()
    for r in refs:
        if not isinstance(r, dict) or not {"column", "parent_table", "parent_key", "source_column"} <= set(r):
            raise LoadError("each ref needs column, parent_table, parent_key and source_column")
        if r["column"] not in real:
            raise LoadError(f"ref column {r['column']!r} is not a column of {table}")
        if r["column"] in columns:
            raise LoadError(f"{r['column']!r} is filled by a ref, so it must not also be in columns")
        if r["source_column"] not in columns:
            raise LoadError(f"ref source_column {r['source_column']!r} must be one of columns")
        if r["parent_table"] not in state:
            raise LoadError(f"ref parent_table {r['parent_table']!r} does not exist in {schema!r}")
        carriers.add(r["source_column"])
        ref_cols.add(r["column"])
    unknown = [c for c in columns if c not in real and c not in carriers]
    if unknown:
        raise LoadError(f"{table} has no column {', '.join(unknown)}")
    pk = state[table].get("pk")   # snapshot does not say "identity"; a pk left out is generated or the dry run says so
    missing = [n for n, c in real.items() if n != pk
               and not c["nullable"] and c["default"] is None and n not in columns and n not in ref_cols]
    if missing:
        raise LoadError(f"NOT NULL column(s) without a default must be loaded: {', '.join(missing)}")

    cfg = _cfg()
    if StagedLoad.objects.filter(owner=user, status=StagedLoad.Status.OPEN).count() >= cfg["maxOpenPerUser"]:
        raise LoadError(f"you already have {cfg['maxOpenPerUser']} open loads; finish or cancel one first")
    name = f"{uuid.uuid4().hex}.copy"
    fd = os.open(os.path.join(storage.loads_dir(), name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    now = timezone.now()
    try:
        load = StagedLoad.objects.create(
            owner=user, schema=schema, table=table, columns=columns, refs=refs, path=name, created_at=now,
            expires_at=now + timedelta(hours=cfg["ttlHours"]))
    except BaseException:
        os.unlink(os.path.join(storage.loads_dir(), name))
        raise
    record(user, "load.begin", f"{schema}.{table}", {"load_id": load.pk, "columns": columns})
    return {"load_id": load.pk, "table": table, "schema": schema, "columns": columns, "next_seq": 1,
            "limits": _limits(load)}


def get(user, load_id: int) -> dict:
    load = _mine(user, load_id)
    return {"load_id": load.pk, "status": load.status, "schema": load.schema, "table": load.table,
            "columns": load.columns, "next_seq": load.next_seq, "total_rows": load.rows,
            "total_bytes": load.bytes, "expires_at": load.expires_at.isoformat(), "change_id": load.change_id}


def cancel(user, load_id: int) -> dict:
    load = _mine(user, load_id)
    if load.status == StagedLoad.Status.FINISHED:
        raise LoadError(f"load #{load.pk} is already finished (change #{load.change_id}); it cannot be cancelled")
    if load.status == StagedLoad.Status.OPEN:
        load.status = StagedLoad.Status.CANCELLED
        _remove_file(load)
        load.save(update_fields=["status", "path"])
        record(user, "load.cancel", f"{load.schema}.{load.table}", {"load_id": load.pk, "rows": load.rows})
    return {"load_id": load.pk, "status": load.status}
