import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
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
    if tok.expires_at is not None and tok.expires_at < timezone.now():
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


ACCESS_TTL = timedelta(days=30)


def _issue_pair(tok: McpToken) -> tuple[str, str]:
    raw = PREFIX + secrets.token_urlsafe(32)
    refresh = "sgr_" + secrets.token_urlsafe(32)
    tok.token_hash, tok.prefix, tok.refresh_hash = _hash(raw), raw[:10], _hash(refresh)
    tok.expires_at = timezone.now() + ACCESS_TTL
    return raw, refresh


def create_oauth_token(user: User, client_name: str) -> tuple[McpToken, str, str]:
    """The tokens an OAuth client receives: an access token (an ordinary revocable token shown in the Connect tab,
    valid for 30 days) and a refresh token that renews it. A new sign-in by the same client replaces its previous
    one, and it does not count against the manual-token cap."""
    name = f"OAuth: {client_name}"[:64]
    for old in McpToken.objects.filter(user=user, name=name, revoked_at__isnull=True):
        old.revoked_at = timezone.now()
        old.save(update_fields=["revoked_at"])
    tok = McpToken(user=user, name=name)
    raw, refresh = _issue_pair(tok)
    tok.save()
    record(user, "token.oauth_issued", target=name)
    return tok, raw, refresh


def refresh_oauth_token(refresh_raw: str) -> tuple[str, str] | None:
    """Swap a refresh token for a new access + refresh pair (the old pair stops working). None if unknown/revoked."""
    if not refresh_raw or not refresh_raw.startswith("sgr_"):
        return None
    with transaction.atomic():
        tok = (McpToken.objects.select_for_update().select_related("user")
               .filter(refresh_hash=_hash(refresh_raw), revoked_at__isnull=True).first())
        if tok is None or not tok.user.is_active:
            return None
        raw, refresh = _issue_pair(tok)
        tok.save(update_fields=["token_hash", "prefix", "refresh_hash", "expires_at"])
    return raw, refresh
