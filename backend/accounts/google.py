"""Thin wrapper around Google's OAuth authorization-code flow.

Kept separate so tests can replace `fetch_claims` without network access.
"""
from urllib.parse import urlencode

import requests
from django.conf import settings
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleAuthError(Exception):
    pass


def authorization_url(redirect_uri: str, state: str, hosted_domain: str | None = None) -> str:
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "offline",
    }
    if hosted_domain:
        params["hd"] = hosted_domain
    return f"{AUTH_URL}?{urlencode(params)}"


def fetch_claims(code: str, redirect_uri: str) -> dict:
    """Exchange the code for tokens and return verified ID-token claims."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise GoogleAuthError("token exchange failed")
    raw = resp.json().get("id_token")
    if not raw:
        raise GoogleAuthError("no id_token in response")
    try:
        return id_token.verify_oauth2_token(
            raw, google_requests.Request(), settings.GOOGLE_OAUTH_CLIENT_ID
        )
    except ValueError as exc:
        raise GoogleAuthError("invalid id_token") from exc
