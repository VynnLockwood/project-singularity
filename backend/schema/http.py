"""Shared HTTP plumbing for views that take a `?schema=` parameter."""
from functools import wraps

from accounts.api import error

from . import introspect


def _valid(value, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise introspect.UnknownSchema(f"schema in the {where} must be a non-empty string.")
    return value


ALL = "*"


def schema_param(request, body: dict | None = None, default: str | None = "master", *, allow_all: bool = False) -> str | None:
    """The one rule for a `schema` argument: ABSENT means `default` (master), anything present must be a
    non-empty string. Empty, non-string or conflicting values (repeated `?schema=`, or query vs JSON `body`) raise
    UnknownSchema, which `unknown_schema_is_400` turns into HTTP 400.

    Pass `default=None` where absent means "all schemas" (the changes history list). With `allow_all=True`
    (duplicates list and search only) an explicit `*` means all schemas and the caller gets None; absent is still
    `default`. Everywhere else `*` is just an unknown schema, so a 400.
    """
    found = []
    if "schema" in request.GET:
        found += [_valid(v, "query string") for v in request.GET.getlist("schema")]
    if body is not None and "schema" in body:
        found.append(_valid(body["schema"], "request body"))
    if len(set(found)) > 1:
        raise introspect.UnknownSchema("conflicting schema values were given.")
    if found and found[0] == ALL and allow_all:
        return None
    return found[0] if found else default


def schema_for(request, level: str = "viewer", body: dict | None = None, default: str = "master") -> str:
    """`schema_param` (absent means `default`, master) checked against the user's access at `level`.

    A database the user cannot reach raises the same `UnknownSchema` as one that does not exist; a readable one
    below `level` raises `access.AccessDenied`. Call it first in a view, before anything that looks the database
    or its tables up, so the answers cannot differ.
    """
    from accounts import access

    return access.require(request.user, schema_param(request, body, default), level)


def unknown_schema_is_400(fn):
    """Decorator for views: an unmanaged, unknown or unreachable schema anywhere below becomes HTTP 400, never
    500; a readable database where the user lacks the level (`access.AccessDenied`) becomes 403.

    Put it inside `@endpoint(...)` so sign-in and role checks still run first.
    """
    from accounts import access

    @wraps(fn)
    def wrapper(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except introspect.UnknownSchema as exc:
            return error(str(exc), 400)
        except access.AccessDenied as exc:
            return error(str(exc), exc.status)

    return wrapper
