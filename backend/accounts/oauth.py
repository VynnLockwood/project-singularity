"""OAuth 2.1 for MCP clients (authorization code + PKCE S256, dynamic client registration).

Connectors such as Claude.ai discover this through /.well-known, register themselves, send the person to
/oauth/authorize (which uses the normal Google sign-in), and swap the code for an access token. The access
token is the same kind of revocable `sgl_...` token the Connect tab makes.
"""
import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from . import tokens
from .models import OAuthClient, OAuthCode, User

CODE_TTL = timedelta(minutes=10)


class OAuthError(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(description or code)
        self.code, self.description = code, description


def _redirect_ok(uri) -> bool:
    if not isinstance(uri, str) or len(uri) > 500:
        return False
    u = urlparse(uri)
    if u.fragment or not u.netloc:
        return False
    local = u.hostname in ("localhost", "127.0.0.1", "[::1]", "::1")
    return u.scheme == "https" or (u.scheme == "http" and local)


def register_client(body: dict) -> OAuthClient:
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not (1 <= len(uris) <= 10) or not all(_redirect_ok(u) for u in uris):
        raise OAuthError("invalid_redirect_uri", "redirect_uris must be 1 to 10 https (or localhost http) URLs")
    name = str(body.get("client_name") or "MCP client").strip()[:100] or "MCP client"
    return OAuthClient.objects.create(client_id="sgc_" + secrets.token_urlsafe(18), name=name, redirect_uris=uris)


def get_client(client_id: str, redirect_uri: str) -> OAuthClient:
    client = OAuthClient.objects.filter(client_id=client_id or "").first()
    if client is None:
        raise OAuthError("invalid_client", "unknown client_id")
    if redirect_uri not in client.redirect_uris:
        raise OAuthError("invalid_request", "redirect_uri does not match the registered one")
    return client


def issue_code(client: OAuthClient, user: User, redirect_uri: str, challenge: str) -> str:
    raw = secrets.token_urlsafe(32)
    OAuthCode.objects.create(
        code_hash=hashlib.sha256(raw.encode()).hexdigest(), client=client, user=user,
        redirect_uri=redirect_uri, code_challenge=challenge, expires_at=timezone.now() + CODE_TTL,
    )
    OAuthCode.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    return raw


def exchange(code: str, client_id: str, redirect_uri: str, verifier: str) -> tuple[str, str]:
    """Swap an authorization code for (access token, refresh token). A code works once."""
    with transaction.atomic():
        row = (
            OAuthCode.objects.select_for_update().select_related("client", "user")
            .filter(code_hash=hashlib.sha256((code or "").encode()).hexdigest()).first()
        )
        if row is None or row.used or row.expires_at < timezone.now():
            raise OAuthError("invalid_grant", "the code is invalid or expired")
        row.used = True
        row.save(update_fields=["used"])
    if row.client.client_id != client_id or row.redirect_uri != redirect_uri:
        raise OAuthError("invalid_grant", "the code was issued to a different client or redirect")
    expected = base64.urlsafe_b64encode(hashlib.sha256((verifier or "").encode()).digest()).rstrip(b"=").decode()
    if not verifier or not secrets.compare_digest(expected, row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    if not row.user.is_active:
        raise OAuthError("invalid_grant", "the account is disabled")
    _, access, refresh = tokens.create_oauth_token(row.user, row.client.name)
    return access, refresh
