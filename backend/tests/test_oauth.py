import base64
import hashlib
import json

import pytest
from django.test import Client

from accounts import tokens
from accounts.models import OAuthClient, Role, User

pytestmark = pytest.mark.django_db
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def pkce(verifier="v" * 50):
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def register(c, **over):
    body = {"redirect_uris": [REDIRECT], "client_name": "Claude", **over}
    return c.post("/oauth/register", json.dumps(body), content_type="application/json")


def authorize_url(client_id, challenge, **over):
    from urllib.parse import urlencode
    q = {"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": challenge,
         "code_challenge_method": "S256", "state": "xyz", **over}
    return "/oauth/authorize?" + urlencode(q)


def test_discovery_documents_point_at_our_endpoints():
    c = Client(HTTP_HOST="testserver")
    pr = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"].endswith("/mcp") and pr["authorization_servers"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/oauth/register")
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]


def test_registration_validates_redirects():
    c = Client()
    ok = register(c)
    assert ok.status_code == 201 and ok.json()["client_id"].startswith("sgc_")
    assert register(c, redirect_uris=["http://evil.example.com/cb"]).status_code == 400
    assert register(c, redirect_uris=[]).status_code == 400
    assert register(c, redirect_uris=["http://localhost:6274/cb"]).status_code == 201


def test_full_flow_gives_a_working_revocable_token():
    user = User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)
    cid = register(Client()).json()["client_id"]
    verifier, challenge = pkce()

    browser = Client()
    browser.force_login(user)
    page = browser.get(authorize_url(cid, challenge))
    assert page.status_code == 200 and b"Claude" in page.content and b"e@x.com" in page.content

    back = browser.post(authorize_url(cid, challenge), {"decision": "allow"})
    assert back.status_code == 302 and back["Location"].startswith(REDIRECT + "?")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(back["Location"]).query)
    assert q["state"] == ["xyz"]

    tok = Client().post("/oauth/token", {"grant_type": "authorization_code", "code": q["code"][0], "client_id": cid,
                                         "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert tok.status_code == 200
    access = tok.json()["access_token"]
    assert tokens.authenticate(access) == user  # works as an MCP bearer token

    # the code is single use
    again = Client().post("/oauth/token", {"grant_type": "authorization_code", "code": q["code"][0], "client_id": cid,
                                           "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_wrong_verifier_or_redirect_is_refused():
    user = User.objects.create(username="e@x.com", email="e@x.com", role=Role.VIEWER)
    cid = register(Client()).json()["client_id"]
    verifier, challenge = pkce()
    browser = Client()
    browser.force_login(user)

    def get_code():
        loc = browser.post(authorize_url(cid, challenge), {"decision": "allow"})["Location"]
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(loc).query)["code"][0]

    bad = Client().post("/oauth/token", {"grant_type": "authorization_code", "code": get_code(), "client_id": cid,
                                         "redirect_uri": REDIRECT, "code_verifier": "wrong" * 12})
    assert bad.status_code == 400
    other = Client().post("/oauth/token", {"grant_type": "authorization_code", "code": get_code(), "client_id": cid,
                                           "redirect_uri": "https://other.example/cb", "code_verifier": verifier})
    assert other.status_code == 400


def test_deny_and_bad_requests_do_not_issue_codes():
    user = User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)
    cid = register(Client()).json()["client_id"]
    _, challenge = pkce()
    b = Client()
    b.force_login(user)
    denied = b.post(authorize_url(cid, challenge), {"decision": "deny"})
    assert "error=access_denied" in denied["Location"]
    # unknown redirect_uri: an error page, never a redirect
    bad = b.get(authorize_url(cid, challenge, redirect_uri="https://evil.example/cb"))
    assert bad.status_code == 400
    # PKCE is mandatory
    nop = b.get(authorize_url(cid, challenge, code_challenge_method="plain"))
    assert "invalid_request" in nop["Location"]


def test_signed_out_users_sign_in_first_and_come_back(settings):
    cid = register(Client()).json()["client_id"]
    _, challenge = pkce()
    anon = Client()
    url = authorize_url(cid, challenge)
    r = anon.get(url)
    assert r.status_code == 302 and r["Location"] == "/api/auth/google/login"
    assert anon.session["post_login"] == url


def test_callback_returns_to_the_consent_page(monkeypatch):
    from accounts import google
    monkeypatch.setattr(google, "fetch_claims", lambda code, uri: {"email": "e@x.com", "email_verified": True, "sub": "1", "hd": None})
    monkeypatch.setattr("accounts.views.sign_in_from_claims", lambda claims, cfg: User.objects.get_or_create(
        username="e@x.com", defaults={"email": "e@x.com", "role": Role.EDITOR})[0])
    c = Client()
    s = c.session
    s["google_oauth_state"] = "st"
    s["post_login"] = "/oauth/authorize?client_id=a&state=b"
    s.save()
    r = c.get("/api/auth/google/callback?state=st&code=abc")
    assert r["Location"] == "/oauth/authorize?client_id=a&state=b"
    # an arbitrary path is not honoured
    s = c.session
    s["google_oauth_state"] = "st2"
    s["post_login"] = "https://evil.example/"
    s.save()
    assert c.get("/api/auth/google/callback?state=st2&code=abc")["Location"] == "/"


def test_new_oauth_token_replaces_the_clients_previous_one():
    user = User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)
    _, first, _r1 = tokens.create_oauth_token(user, "Claude")
    _, second, _r2 = tokens.create_oauth_token(user, "Claude")
    assert tokens.authenticate(first) is None and tokens.authenticate(second) == user


def test_tokens_expire_and_refresh_issues_a_new_pair():
    from datetime import timedelta

    from django.utils import timezone

    user = User.objects.create(username="r@x.com", email="r@x.com", role=Role.EDITOR)
    row, access, refresh = tokens.create_oauth_token(user, "Claude")
    assert tokens.authenticate(access) == user
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save()
    assert tokens.authenticate(access) is None  # expired
    new_access, new_refresh = tokens.refresh_oauth_token(refresh)
    assert tokens.authenticate(new_access) == user and new_refresh != refresh and tokens.authenticate(access) is None
    assert tokens.refresh_oauth_token(refresh) is None  # the old refresh token is spent
    row.refresh_from_db()
    row.revoked_at = timezone.now()
    row.save()
    assert tokens.refresh_oauth_token(new_refresh) is None and tokens.authenticate(new_access) is None  # revoked


def test_token_endpoint_returns_expiry_and_a_refresh_token_that_works():
    from accounts.models import OAuthClient

    user = User.objects.create(username="t@x.com", email="t@x.com", role=Role.EDITOR)
    _, _a, refresh = tokens.create_oauth_token(user, "Claude")
    r = Client().post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": refresh})
    body = r.json()
    assert r.status_code == 200 and body["expires_in"] > 0 and body["refresh_token"] and tokens.authenticate(body["access_token"]) == user
    bad = Client().post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": refresh})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert "refresh_token" in Client().get("/.well-known/oauth-authorization-server").json()["grant_types_supported"]
    assert OAuthClient is not None
