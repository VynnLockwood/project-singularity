"""Shared plumbing for JSON endpoints: method, sign-in, role and body checks."""
import json
from functools import wraps

from django.http import JsonResponse

ROLE_CHECK = {
    "viewer": lambda u: u.can_read,
    "editor": lambda u: u.can_edit,
    "approver": lambda u: u.can_approve,
}


def error(message, status=400, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def endpoint(methods, min_role="viewer"):
    """Decorator: allowed HTTP methods, signed-in user with at least `min_role`, JSON body in request.json."""
    methods = {methods} if isinstance(methods, str) else set(methods)

    def deco(fn):
        @wraps(fn)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error("method not allowed", 405)
            user = request.user
            if not user.is_authenticated:
                return error("unauthenticated", 401)
            if not ROLE_CHECK[min_role](user):
                return error("forbidden", 403)
            request.json = {}
            if request.method in ("POST", "PATCH", "PUT") and request.body:
                try:
                    request.json = json.loads(request.body)
                except json.JSONDecodeError:
                    return error("invalid json", 400)
                if not isinstance(request.json, dict):
                    return error("body must be a JSON object", 400)
            return fn(request, *args, **kwargs)

        return wrapper

    return deco


def limit_param(request, default=50, maximum=200) -> int:
    try:
        return max(1, min(int(request.GET.get("limit", default)), maximum))
    except ValueError:
        return default
