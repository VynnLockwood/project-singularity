"""pg_dump of a managed schema before every applied schema change (spec section 6)."""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings


class BackupError(Exception):
    pass


SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
DUMP_RE = re.compile(r"^[a-z][a-z0-9_]*-\d{8}T\d{6}Z-.*\.dump$")


def backup_master(label: str, retention_days: int = 14) -> Path:
    return backup_schema("master", label, retention_days)


def backup_schema(schema: str, label: str, retention_days: int = 14) -> Path:
    if not isinstance(schema, str) or not SCHEMA_RE.match(schema):
        raise BackupError(f"invalid schema name {schema!r}")
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", label) or ".." in label:
        raise BackupError(f"invalid backup label {label!r}")
    c = settings.MASTER_DB
    folder = Path(settings.BACKUP_DIR)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create backup folder {folder}: {exc}") from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{schema}-{stamp}-{label}.dump"
    # Read-only role: a backup must never be able to change anything.
    env = {**os.environ, "PGPASSWORD": c["ro_password"] or ""}
    cmd = [
        "pg_dump", "-h", str(c["host"]), "-p", str(c["port"]), "-U", c["ro_user"], "-d", c["dbname"],
        f"--schema={schema}", "--format=custom", "--file", str(path),
    ]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise BackupError("pg_dump is not installed") from exc
    except subprocess.CalledProcessError as exc:
        path.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        path.unlink(missing_ok=True)
        raise BackupError("pg_dump timed out") from exc
    prune(folder, retention_days)
    return path


_FILE_RE = re.compile(r"^(?P<schema>[a-z][a-z0-9_]*)-(?P<stamp>\d{8}T\d{6}Z)-(?P<label>.+)\.dump$")


def describe(path: Path) -> dict | None:
    """One backup file as {file, schema, label, bytes, created_at}; None if it is not a backup this module wrote."""
    m = _FILE_RE.match(path.name)
    if m is None or not DUMP_RE.match(path.name) or not path.is_file():
        return None
    created = datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"file": path.name, "schema": m["schema"], "label": m["label"], "bytes": path.stat().st_size,
            "created_at": created}


def list_backups(schema: str | None = None) -> list[dict]:
    """The backups in BACKUP_DIR, newest first, optionally only one schema's. A missing folder is an empty list."""
    folder = Path(settings.BACKUP_DIR)
    if not folder.is_dir():
        return []
    found = [d for p in folder.iterdir() if (d := describe(p)) is not None and (schema is None or d["schema"] == schema)]
    return sorted(found, key=lambda d: (d["created_at"], d["file"]), reverse=True)


def prune(folder: Path, retention_days: int) -> int:
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in folder.glob("*-*.dump"):
        if not DUMP_RE.match(f.name):
            continue
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed
