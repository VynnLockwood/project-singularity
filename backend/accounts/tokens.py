import hashlib
import secrets

from django.utils import timezone

from audit.models import record

from .models import McpToken, User

PREFIX = "sgl_"


class TokenError(Exception):
    pass


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_token(user: User, name: str) -> tuple[McpToken, str]:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise TokenError("give the token a name (max 64 characters)")
    if McpToken.objects.filter(user=user, revoked_at__isnull=True).count() >= 20:
        raise TokenError("too many active tokens; revoke one first")
    raw = PREFIX + secrets.token_urlsafe(32)
    tok = McpToken.objects.create(user=user, name=name, token_hash=_hash(raw), prefix=raw[:10])
    record(user, "token.created", target=name)
    return tok, raw


def authenticate(raw: str) -> User | None:
    """Return the user for a raw bearer token, or None if unknown, revoked or disabled."""
    if not raw or not raw.startswith(PREFIX):
        return None
    tok = McpToken.objects.select_related("user").filter(token_hash=_hash(raw), revoked_at__isnull=True).first()
    if tok is None or not tok.user.is_active:
        return None
    McpToken.objects.filter(pk=tok.pk).update(last_used_at=timezone.now())
    return tok.user


def revoke(user: User, token_id: int) -> None:
    tok = McpToken.objects.filter(pk=token_id, user=user, revoked_at__isnull=True).first()
    if tok is None:
        raise TokenError("token not found")
    tok.revoked_at = timezone.now()
    tok.save(update_fields=["revoked_at"])
    record(user, "token.revoked", target=tok.name)
