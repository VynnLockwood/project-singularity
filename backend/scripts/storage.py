"""Script file storage: normalize, hash, write under SCRIPT_DIR with random names, read with tamper check, purge."""
import hashlib
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
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
    if name in ("", ".", ".."):
        name = "script.sql"
    return name[:NAME_MAX]


def _full_path(rel: str) -> str:
    if not rel or os.path.basename(rel) != rel or rel in (".", ".."):
        raise ScriptError("Invalid stored script path.")
    return os.path.join(settings.SCRIPT_DIR, rel)


def _ensure_dir() -> None:
    d = settings.SCRIPT_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.stat(d)
    if st.st_uid == os.geteuid():
        os.chmod(d, 0o700)
    elif st.st_mode & 0o077:
        raise ScriptError("Script directory is not private.")


def loads_dir() -> str:
    """Private directory for staged-load files (SCRIPT_DIR/loads), created on first use."""
    _ensure_dir()
    d = os.path.join(settings.SCRIPT_DIR, "loads")
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.stat(d).st_uid == os.geteuid():
        os.chmod(d, 0o700)
    return d


def _write_atomic(rel: str, data: bytes) -> None:
    """Write to a temp file in the same directory, fsync, then rename into place. Nothing is left behind on failure."""
    final = _full_path(rel)
    tmp = os.path.join(settings.SCRIPT_DIR, f".tmp-{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        for leftover in (tmp, final):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
        raise


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
    if "\x00" in text:
        raise ScriptError("Script contains NUL bytes.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ScriptError("Script is empty.")
    stored = text.encode("utf-8")

    _ensure_dir()
    rel = f"{uuid.uuid4().hex}.sql"
    _write_atomic(rel, stored)

    now = timezone.now()
    try:
        return Script.objects.create(
            owner=owner, name=_clean_name(name), sha256=hashlib.sha256(stored).hexdigest(),
            size=len(stored), path=rel, created_at=now, expires_at=now + SCRIPT_TTL,
        )
    except BaseException:
        try:
            os.unlink(_full_path(rel))
        except FileNotFoundError:
            pass
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


def _candidate_ids(now) -> list[int]:
    return list(
        Script.objects.filter(expires_at__lte=now).exclude(status=Script.Status.EXPIRED)
        .exclude(change__status="pending").values_list("id", flat=True)
    )


def purge_expired(now=None) -> int:
    """Delete files of scripts past expires_at and mark them expired. Scripts tied to a pending change are kept.

    Each row is locked (skipping rows another transaction holds) and its conditions are re-checked under the lock,
    so a script submitted after the listing is never destroyed.
    """
    now = now or timezone.now()
    count = 0
    for pk in _candidate_ids(now):
        with transaction.atomic():
            script = (
                Script.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("change").filter(pk=pk).first()
            )
            if script is None or script.status == Script.Status.EXPIRED or script.expires_at > now:
                continue
            if script.change_id and script.change.status == "pending":
                continue
            updated = Script.objects.filter(pk=pk, status=script.status).update(
                status=Script.Status.EXPIRED, path=""
            )
            if not updated:
                continue
            if script.path:
                try:
                    os.unlink(_full_path(script.path))
                except FileNotFoundError:
                    pass
            count += 1
    return count
