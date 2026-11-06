"""Script file storage: normalize, hash, write under SCRIPT_DIR with random names, read with tamper check, purge."""
import hashlib
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from config.siconfig import get_config

from .models import Script

SCRIPT_TTL = timedelta(days=7)
BOM = b"\xef\xbb\xbf"
NAME_MAX = 200


class ScriptError(Exception):
    pass


def _clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (name or "script.sql")[:NAME_MAX]


def _full_path(rel: str) -> str:
    return os.path.join(settings.SCRIPT_DIR, rel)


def save(owner, name: str, data: bytes) -> Script:
    """Store an upload. The size limit (limits.maxScriptBytes) applies to the RAW upload size, before normalizing."""
    if len(data) > get_config()["limits"]["maxScriptBytes"]:
        raise ScriptError("Script is larger than the allowed size.")
    if data.startswith(BOM):
        data = data[len(BOM):]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ScriptError("Script is not valid UTF-8 text.") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ScriptError("Script is empty.")
    stored = text.encode("utf-8")

    os.makedirs(settings.SCRIPT_DIR, mode=0o700, exist_ok=True)
    rel = f"{uuid.uuid4().hex}.sql"
    fd = os.open(_full_path(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(stored)

    now = timezone.now()
    try:
        return Script.objects.create(
            owner=owner, name=_clean_name(name), sha256=hashlib.sha256(stored).hexdigest(),
            size=len(stored), path=rel, created_at=now, expires_at=now + SCRIPT_TTL,
        )
    except Exception:
        os.unlink(_full_path(rel))
        raise


def read(script: Script) -> bytes:
    if script.status == Script.Status.EXPIRED or not script.path:
        raise ScriptError("Script has expired.")
    try:
        with open(_full_path(script.path), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise ScriptError("Script file is missing.") from None
    if hashlib.sha256(data).hexdigest() != script.sha256:
        raise ScriptError("Script file does not match its checksum.")
    return data


def purge_expired(now=None) -> int:
    """Delete files of scripts past expires_at and mark them expired. Scripts tied to a pending change are kept."""
    now = now or timezone.now()
    due = Script.objects.filter(expires_at__lte=now).exclude(status=Script.Status.EXPIRED)
    due = due.exclude(change__status="pending")
    count = 0
    for script in due:
        if script.path:
            try:
                os.unlink(_full_path(script.path))
            except FileNotFoundError:
                pass
        script.status = Script.Status.EXPIRED
        script.path = ""
        script.save(update_fields=["status", "path"])
        count += 1
    return count
