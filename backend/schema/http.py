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


def unknown_schema_is_400(fn):
    """Decorator for views: an unmanaged or unknown schema anywhere below becomes HTTP 400, never 500.

    Put it inside `@endpoint(...)` so sign-in and role checks still run first.
    """
    @wraps(fn)
    def wrapper(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except introspect.UnknownSchema as exc:
            return error(str(exc), 400)

    return wrapper
