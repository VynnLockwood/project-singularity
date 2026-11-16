import secrets

from django.contrib.auth import login, logout
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from config.siconfig import get_config

from . import capabilities, google
from . import tokens as token_service
from .api import endpoint
from .models import User
from .services import (
    NotAdmin,
    SignInDenied,
    UserManagementDenied,
    change_role,
    create_user,
    set_user_active,
    sign_in_from_claims,
)

STATE_KEY = "google_oauth_state"


def _user_json(user: User) -> dict:
    return {
        "id": user.pk,
        "email": user.email,
        "name": f"{user.first_name} {user.last_name}".strip(),
        "role": user.role,
        "active": user.is_active,
        "lastLoginAt": user.last_login.isoformat() if user.last_login else None,
    }


def _redirect_uri(request) -> str:
    return request.build_absolute_uri("/api/auth/google/callback")


@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({"ok": True})


@require_GET
def me(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "unauthenticated"}, status=401)
    return JsonResponse({**_user_json(request.user), "capabilities": capabilities.build(request.user)})


@require_GET
def google_login(request):
    state = secrets.token_urlsafe(32)
    request.session[STATE_KEY] = state
    domains = get_config()["auth"]["allowedDomains"]
    # Only pass hd for Google Workspace domains, not consumer gmail.com
    hd = domains[0] if len(domains) == 1 and domains[0] != "gmail.com" else None
    return HttpResponseRedirect(google.authorization_url(_redirect_uri(request), state, hd))


@require_GET
def google_callback(request):
    expected = request.session.pop(STATE_KEY, None)
    given = request.GET.get("state", "")
    code = request.GET.get("code", "")
    if not expected or not secrets.compare_digest(expected, given) or not code:
        return JsonResponse({"error": "invalid oauth state"}, status=400)
    try:
        claims = google.fetch_claims(code, _redirect_uri(request))
        user = sign_in_from_claims(claims, get_config()["auth"])
    except google.GoogleAuthError:
        return JsonResponse({"error": "google sign-in failed"}, status=400)
    except SignInDenied:
        return JsonResponse({"error": "not allowed"}, status=403)
    login(request, user)
    # an OAuth connector sent them here to sign in first: take them back to its consent page
    nxt = request.session.pop("post_login", "")
    return HttpResponseRedirect(nxt if nxt.startswith("/oauth/authorize?") else "/")


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({"ok": True})


def _refused(exc: UserManagementDenied):
    """A refused user-management action: 403 when the actor may not manage users at all, otherwise 400."""
    return JsonResponse({"error": str(exc)}, status=403 if isinstance(exc, NotAdmin) else 400)


def _apply(actor, target, body):
    """Apply the optional role and active fields of a PATCH; returns an error response or None."""
    role, active = body.get("role"), body.get("active")
    if role is None and active is None:
        return JsonResponse({"error": "nothing to change"}, status=400)
    if active is not None and not isinstance(active, bool):
        return JsonResponse({"error": "active must be true or false"}, status=400)
    if role is not None and not isinstance(role, str):
        return JsonResponse({"error": "unknown role"}, status=400)
    try:
        with transaction.atomic():  # both steps or neither
            if role is not None:
                change_role(actor, target, role)
            if active is not None:
                set_user_active(actor, target, active)
    except UserManagementDenied as exc:
        return _refused(exc)
    return None


@endpoint(["GET", "POST"], min_role="admin")
def users_list(request):
    if request.method == "GET":
        return JsonResponse({"users": [_user_json(u) for u in User.objects.order_by("email")]})
    body = request.json
    name = body.get("name") or ""
    if not isinstance(name, str):
        return JsonResponse({"error": "name must be text"}, status=400)
    first, _, last = name.strip().partition(" ")
    try:
        user = create_user(request.user, body.get("email"), body.get("role"), first_name=first, last_name=last.strip())
    except UserManagementDenied as exc:
        return _refused(exc)
    return JsonResponse(_user_json(user), status=201)


@endpoint("PATCH", min_role="admin")
def user_detail(request, user_id: int):
    target = User.objects.filter(pk=user_id).first()
    if target is None:
        return JsonResponse({"error": "not found"}, status=404)
    err = _apply(request.user, target, request.json)
    if err:
        return err
    target.refresh_from_db()
    return JsonResponse(_user_json(target))


@endpoint("PATCH", min_role="admin")
def user_role(request, user_id: int):
    """The older role-only route, kept for the Settings page; same service as PATCH /api/users/<id>."""
    target = User.objects.filter(pk=user_id).first()
    if target is None:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        change_role(request.user, target, request.json.get("role", ""))
    except UserManagementDenied as exc:
        return _refused(exc)
    target.refresh_from_db()
    return JsonResponse(_user_json(target))


# ---- MCP access tokens (each user manages their own) -------------------------

@endpoint(["GET", "POST"], min_role="editor")
def mcp_tokens(request):
    if request.method == "GET":
        return JsonResponse({"tokens": [
            {"id": t.pk, "name": t.name, "prefix": t.prefix, "createdAt": t.created_at.isoformat(),
             "lastUsedAt": t.last_used_at.isoformat() if t.last_used_at else None}
            for t in request.user.mcp_tokens.filter(revoked_at__isnull=True)
        ]})
    try:
        tok, raw = token_service.create_token(request.user, request.json.get("name", ""))
    except token_service.TokenError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    # The only time the raw token is ever shown.
    return JsonResponse({"id": tok.pk, "name": tok.name, "prefix": tok.prefix, "token": raw}, status=201)


@endpoint("DELETE", min_role="editor")
def mcp_token_detail(request, token_id):
    try:
        token_service.revoke(request.user, token_id)
    except token_service.TokenError:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse({"ok": True})
