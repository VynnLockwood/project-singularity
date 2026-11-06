import html
import json
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import oauth, tokens
from .oauth import OAuthError


def _origin(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def _cors(resp):
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Access-Control-Allow-Headers"] = "content-type, authorization, mcp-protocol-version"
    resp["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _json_error(code, description="", status=400):
    return _cors(JsonResponse({"error": code, "error_description": description}, status=status))


@require_http_methods(["GET", "OPTIONS"])
def protected_resource(request, suffix=None):
    o = _origin(request)
    return _cors(JsonResponse({
        "resource": f"{o}/mcp", "authorization_servers": [o], "bearer_methods_supported": ["header"],
        "resource_name": "Singularity",
    }))


@require_http_methods(["GET", "OPTIONS"])
def authorization_server(request, suffix=None):
    o = _origin(request)
    return _cors(JsonResponse({
        "issuer": o,
        "authorization_endpoint": f"{o}/oauth/authorize",
        "token_endpoint": f"{o}/oauth/token",
        "registration_endpoint": f"{o}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def register(request):
    if request.method == "OPTIONS":
        return _cors(HttpResponse(status=204))
    try:
        body = json.loads(request.body or b"{}")
        if not isinstance(body, dict):
            raise ValueError
        client = oauth.register_client(body)
    except (ValueError, json.JSONDecodeError):
        return _json_error("invalid_client_metadata", "body must be a JSON object")
    except OAuthError as exc:
        return _json_error(exc.code, exc.description)
    return _cors(JsonResponse({
        "client_id": client.client_id, "client_name": client.name, "redirect_uris": client.redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none",
    }, status=201))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def token(request):
    if request.method == "OPTIONS":
        return _cors(HttpResponse(status=204))
    f = request.POST
    grant = f.get("grant_type")
    try:
        if grant == "authorization_code":
            access, refresh = oauth.exchange(f.get("code", ""), f.get("client_id", ""), f.get("redirect_uri", ""), f.get("code_verifier", ""))
        elif grant == "refresh_token":
            pair = tokens.refresh_oauth_token(f.get("refresh_token", ""))
            if pair is None:
                raise OAuthError("invalid_grant", "the refresh token is invalid or was revoked")
            access, refresh = pair
        else:
            return _json_error("unsupported_grant_type", "use authorization_code or refresh_token")
    except OAuthError as exc:
        return _json_error(exc.code, exc.description)
    resp = _cors(JsonResponse({
        "access_token": access, "token_type": "Bearer", "scope": "mcp",
        "expires_in": int(tokens.ACCESS_TTL.total_seconds()), "refresh_token": refresh,
    }))
    resp["Cache-Control"] = "no-store"
    return resp


def _back(redirect_uri, state, **params):
    u = urlparse(redirect_uri)
    q = parse_qsl(u.query) + [(k, v) for k, v in params.items()] + ([("state", state)] if state else [])
    return HttpResponseRedirect(urlunparse(u._replace(query=urlencode(q))))


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to Singularity</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(ellipse at 70% 40%,#2a1208,#050201 65%);
color:#fbe9d0;font-family:system-ui,sans-serif}}main{{max-width:30rem;padding:2rem;border:1px solid #5a2c12;border-radius:16px;background:rgba(14,6,3,.85)}}
h1{{margin:.2rem 0 1rem;color:#ffd9a0;letter-spacing:.06em}}p{{line-height:1.5}}.m{{color:#b8926b}}
button{{font:inherit;padding:.6rem 1.3rem;border-radius:999px;border:1px solid #ff9d2e;background:rgba(255,157,46,.12);color:#fbe9d0;cursor:pointer;margin-right:.6rem}}
button.no{{border-color:#5a2c12;background:none}}</style>
<main><h1>Singularity</h1>
<p><strong>{client}</strong> wants to connect to your Singularity account <strong>{email}</strong> ({role}).</p>
<p class="m">It will be able to use the tools your role allows: read the schema and data, and propose changes. Every change still goes through
the rules gate, and schema changes still need an approver. You can revoke it any time in the Connect tab.</p>
<form method="post" action="{action}"><input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">
<button name="decision" value="allow">Allow</button><button class="no" name="decision" value="deny">Cancel</button></form></main>"""


@require_http_methods(["GET", "POST"])
def authorize(request):
    p = request.GET
    redirect_uri, state = p.get("redirect_uri", ""), p.get("state", "")
    try:
        client = oauth.get_client(p.get("client_id", ""), redirect_uri)  # never redirect on these errors
    except OAuthError as exc:
        return HttpResponse(f"Cannot continue: {html.escape(exc.description)}", status=400)
    if p.get("response_type") != "code":
        return _back(redirect_uri, state, error="unsupported_response_type")
    challenge = p.get("code_challenge", "")
    if p.get("code_challenge_method") != "S256" or not challenge or len(challenge) > 128:
        return _back(redirect_uri, state, error="invalid_request", error_description="PKCE with S256 is required")

    if not request.user.is_authenticated:
        request.session["post_login"] = request.get_full_path()
        return HttpResponseRedirect("/api/auth/google/login")

    if request.method == "POST":
        if request.POST.get("decision") != "allow":
            return _back(redirect_uri, state, error="access_denied")
        return _back(redirect_uri, state, code=oauth.issue_code(client, request.user, redirect_uri, challenge))

    page = PAGE.format(
        client=html.escape(client.name), email=html.escape(request.user.email), role=html.escape(request.user.role),
        action=html.escape(request.get_full_path()), csrf=get_token(request),
    )
    resp = HttpResponse(page)
    resp["Cache-Control"] = "no-store"
    resp["X-Frame-Options"] = "DENY"
    return resp
