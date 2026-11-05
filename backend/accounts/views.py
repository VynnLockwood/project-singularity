import json
import secrets

from django.contrib.auth import login, logout
from django.http import HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from config.siconfig import get_config

from . import google
from .models import User
from .services import RoleChangeDenied, SignInDenied, change_role, sign_in_from_claims

STATE_KEY = "google_oauth_state"


def _user_json(user: User) -> dict:
    return {
        "id": user.pk,
        "email": user.email,
        "name": f"{user.first_name} {user.last_name}".strip(),
        "role": user.role,
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
    return JsonResponse(_user_json(request.user))


@require_GET
def google_login(request):
    state = secrets.token_urlsafe(32)
    request.session[STATE_KEY] = state
    domains = get_config()["auth"]["allowedDomains"]
    hd = domains[0] if len(domains) == 1 else None
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
    return HttpResponseRedirect("/")


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({"ok": True})


@require_GET
def users_list(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "unauthenticated"}, status=401)
    if not request.user.can_approve:
        return JsonResponse({"error": "forbidden"}, status=403)
    users = User.objects.order_by("email")
    return JsonResponse({"users": [_user_json(u) for u in users]})


@require_http_methods(["PATCH"])
def user_role(request, user_id: int):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "unauthenticated"}, status=401)
    if not request.user.can_approve:
        return JsonResponse({"error": "forbidden"}, status=403)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)
    target = User.objects.filter(pk=user_id).first()
    if target is None:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        change_role(request.user, target, body.get("role", ""))
    except RoleChangeDenied as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(_user_json(target))
