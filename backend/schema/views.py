import datetime
import json

from django.http import HttpResponse, JsonResponse

from accounts.api import endpoint, error
from config.siconfig import get_config

from . import introspect, query, rows as rows_mod
from .conn import ro_connection
from .dump_model import read_model
from .export import render_foreign_keys, render_header, render_indexes, render_structure


@endpoint("GET")
def tables(request):
    return JsonResponse({"tables": introspect.list_tables()})


@endpoint("GET")
def graph(request):
    return JsonResponse(introspect.graph())


@endpoint("GET")
def export(request):
    """Download the designed schema as Postgres DDL (`?format=sql` today; the only format)."""
    # Temporary: structure only. Task 4 replaces this with the full export (data, format, schema).
    with ro_connection() as conn:
        model = read_model(conn, "master")
    body = "\n".join([
        render_header("master", False, {}, datetime.date.today()),
        render_structure(model, "master"),
        render_foreign_keys(model, "master"),
        render_indexes(model),
    ])
    resp = HttpResponse(body, content_type="text/sql")
    resp["Content-Disposition"] = 'attachment; filename="singularity-schema.sql"'
    return resp


@endpoint("GET")
def table_detail(request, name):
    d = introspect.describe_table(name)
    if d is None:
        return error("not found", 404)
    return JsonResponse(d)


@endpoint("POST")
def run_query(request):
    limits = get_config()["limits"]
    try:
        limit = min(int(request.json.get("limit", limits["maxQueryRows"])), limits["maxQueryRows"])
    except (TypeError, ValueError):
        return error("limit must be a number")
    try:
        return JsonResponse(query.run_select(
            request.json.get("sql"), limit=limit, timeout_ms=limits["queryTimeoutMs"]))
    except query.QueryError as exc:
        return error(str(exc), 400)


def _rows_error(exc):
    return error(str(exc), exc.status)


@endpoint("GET")
def table_rows(request, name):
    g = request.GET
    try:
        filters = json.loads(g.get("filters", "{}") or "{}")
        if not isinstance(filters, dict):
            raise ValueError
        return JsonResponse(rows_mod.read_rows(
            name, offset=int(g.get("offset", 0)), limit=int(g.get("limit", 50)), sort=g.get("sort") or None,
            direction=g.get("dir", "asc"), filters=filters, include_inactive=g.get("inactive") == "1"))
    except (ValueError, TypeError):
        return error("offset, limit and filters are not valid")
    except rows_mod.RowsError as exc:
        return _rows_error(exc)


@endpoint("GET")
def table_options(request, name):
    try:
        return JsonResponse(rows_mod.options(name, q=request.GET.get("q", ""), limit=int(request.GET.get("limit", 100))))
    except ValueError:
        return error("limit is not valid")
    except rows_mod.RowsError as exc:
        return _rows_error(exc)
