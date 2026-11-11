"""Chunked script upload: begin, put chunks (any order, resend allowed), complete -> Script. Parts live on disk."""
import os
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from config.siconfig import get_config

from . import storage
from .models import UploadSession
from .storage import ScriptError

CHUNK_BYTES = 8 * 2**20
SESSION_TTL = timedelta(hours=24)
MAX_OPEN = 5   # open uploads per user


class Gone(ScriptError):
    """The session is expired or already completed (HTTP 410)."""


class NotFound(ScriptError):
    """No such session for this user (HTTP 404)."""


def begin(user, name: str, size: int) -> dict:
    cap = get_config()["limits"]["maxScriptBytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ScriptError(f"Upload size must be above 0 bytes (got {size!r}).")
    if size > cap:
        raise ScriptError(f"Script is larger than the allowed size: {size} bytes is over the limit of {cap} bytes.")
    purge_expired()
    if UploadSession.objects.filter(owner=user, status=UploadSession.Status.OPEN).count() >= MAX_OPEN:
        raise ScriptError(f"You already have {MAX_OPEN} open uploads. Finish them or wait for them to expire "
                          "(24 hours), then try again.")
    rel = f"{uuid.uuid4().hex}.part"
    path = os.path.join(storage.uploads_dir(), rel)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    now = timezone.now()
    s = UploadSession.objects.create(owner=user, name=storage._clean_name(name), size=size, chunk_bytes=CHUNK_BYTES,
                                     path=rel, created_at=now, expires_at=now + SESSION_TTL)
    return {"upload_id": s.pk, "chunk_bytes": s.chunk_bytes, "chunks": _chunks(s), "max_bytes": cap}


def _chunks(s: UploadSession) -> int:
    return -(-s.size // s.chunk_bytes)


def _get(user, upload_id: int, lock: bool = False) -> UploadSession:
    qs = UploadSession.objects.select_for_update() if lock else UploadSession.objects
    s = qs.filter(pk=upload_id, owner=user).first()
    if s is None:
        raise NotFound("Upload not found.")
    if s.status != UploadSession.Status.OPEN or s.expires_at <= timezone.now():
        raise Gone("This upload has expired or is already complete. Start it again.")
    return s


def _part(s: UploadSession) -> str:
    return os.path.join(storage.uploads_dir(), s.path)


def put_chunk(user, upload_id: int, n: int, data: bytes) -> dict:
    with transaction.atomic():
        s = _get(user, upload_id, lock=True)
        total = _chunks(s)
        if not 0 <= n < total:
            raise ScriptError(f"Chunk {n} is out of range (this upload has chunks 0 to {total - 1}).")
        want = s.chunk_bytes if n < total - 1 else s.size - s.chunk_bytes * (total - 1)
        if len(data) != want:
            raise ScriptError(f"Chunk {n} must be exactly {want} bytes (got {len(data)}).")
        fd = os.open(_part(s), os.O_WRONLY)
        try:
            os.pwrite(fd, data, n * s.chunk_bytes)
        finally:
            os.close(fd)
        if n not in s.received:
            s.received = sorted(s.received + [n])
            s.save(update_fields=["received"])
        return {"received": s.received}


def status(user, upload_id: int) -> dict:
    s = _get(user, upload_id)
    return {"received": sorted(s.received), "size": s.size, "chunks": _chunks(s)}


def complete(user, upload_id: int):
    refused = None
    with transaction.atomic():
        s = _get(user, upload_id, lock=True)
        missing = sorted(set(range(_chunks(s))) - set(s.received))
        if missing:
            shown = ", ".join(map(str, missing[:20])) + (" ..." if len(missing) > 20 else "")
            raise ScriptError(f"Chunks missing: {shown}. Send them, then complete again.")
        part = _part(s)
        try:
            script = storage.save_stream(user, s.name, part)
        except ScriptError as exc:
            refused = exc
        else:
            s.status, s.script = UploadSession.Status.COMPLETE, script
            s.save(update_fields=["status", "script"])
    if refused is not None:
        # the file itself is refused (NUL, not UTF-8, empty, too big): it can never succeed, so free the disk now
        _remove(part)
        UploadSession.objects.filter(pk=upload_id).update(status=UploadSession.Status.EXPIRED, path="")
        raise refused
    _remove(part)
    return script


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def purge_expired(now=None) -> int:
    """Delete parts and rows of expired (or abandoned past expiry) sessions. Returns how many were removed."""
    now = now or timezone.now()
    n = 0
    for s in UploadSession.objects.filter(expires_at__lte=now):
        if s.path:
            _remove(os.path.join(storage.uploads_dir(), s.path))
        s.delete()
        n += 1
    return n
