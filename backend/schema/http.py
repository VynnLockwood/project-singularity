"""Shared HTTP plumbing for views that take a `?schema=` parameter."""
from functools import wraps

from accounts.api import error

from . import introspect


def schema_param(request) -> str:
    """The `schema` query parameter; no parameter (or an empty one) means master."""
    return request.GET.get("schema") or "master"


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
