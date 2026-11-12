import json

from django.http import JsonResponse, StreamingHttpResponse

from accounts.api import ROLE_CHECK, endpoint, error
from audit.models import record
from config.siconfig import get_config

from . import introspect, query, roles, rows as rows_mod
from .dump_model import ExportError
from .export import FORMATS, open_export
from .http import schema_param, unknown_schema_is_400


@endpoint("GET")
def schemas(request):
    """The managed schemas ("databases" in the UI): `{"schemas": [{"name", "tables", "comment"}]}`, master first."""
    return JsonResponse({"schemas": introspect.managed_schemas()})


@endpoint("GET")
@unknown_schema_is_400
def tables(request):
    return JsonResponse({"tables": introspect.list_tables(schema_param(request))})


@endpoint("GET")
@unknown_schema_is_400
def graph(request):
    return JsonResponse(introspect.graph(schema_param(request)))


@endpoint("GET")
@unknown_schema_is_400
def export(request):
    """Download a schema as Postgres SQL: `?schema=master&data=0|1&format=insert|copy`. Data needs the editor role.

    `insert` (default) is plain SQL that any client runs; `copy` is faster but only psql can run it.
    """
    schema = schema_param(request)
    flag = request.GET.get("data", "0")
    if flag not in ("0", "1"):
        return error("data must be 0 or 1", 400)
    with_data = flag == "1"
    fmt = request.GET.get("format", "insert")
    if fmt not in FORMATS:
        return error(f"format must be one of: {', '.join(FORMATS)}", 400)
    if with_data and not ROLE_CHECK["editor"](request.user):
        return error("exporting data needs the editor role", 403)
    try:
        ex = open_export(schema, data=with_data, cap=get_config()["limits"]["maxExportRows"], fmt=fmt)
    except ExportError as exc:
        return error(str(exc), exc.status)
    try:
        record(request.user, "schema.export", schema, {"schema": schema, "data": with_data, "format": fmt, "counts": ex.counts})
        # Under ASGI Django collects a sync iterator into a list first (whole file in memory): hand it an async one.
        chunks = ex.async_chunks() if hasattr(request, "scope") else ex.chunks()
        resp = StreamingHttpResponse(chunks, content_type="text/sql; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{ex.filename()}"'
        resp["Cache-Control"] = "no-store"
    except BaseException:
        ex.close()
        raise
    return resp


@endpoint("GET")
@unknown_schema_is_400
def table_detail(request, name):
    d = introspect.describe_table(name, schema_param(request))
    if d is None:
        return error("not found", 404)
    conv = roles.conventions()
    names = roles.hidden_columns(conv) | set(roles.roles_of(d, conv).values())
    d["hiddenColumns"] = sorted(c["name"] for c in d["columns"] if c["name"] in names)
    return JsonResponse(d)


@endpoint("POST")
@unknown_schema_is_400
def run_query(request):
    limits = get_config()["limits"]
    try:
        limit = min(int(request.json.get("limit", limits["maxQueryRows"])), limits["maxQueryRows"])
    except (TypeError, ValueError):
        return error("limit must be a number")
    try:
        return JsonResponse(query.run_select(
            request.json.get("sql"), limit=limit, timeout_ms=limits["queryTimeoutMs"],
            schema=schema_param(request, request.json)))
    except query.QueryError as exc:
        return error(str(exc), 400)


def _rows_error(exc):
    return error(str(exc), exc.status)


@endpoint("GET")
@unknown_schema_is_400
def table_rows(request, name):
    g = request.GET
    try:
        filters = json.loads(g.get("filters", "{}") or "{}")
        if not isinstance(filters, dict):
            raise ValueError
        return JsonResponse(rows_mod.read_rows(
            name, offset=int(g.get("offset", 0)), limit=int(g.get("limit", 50)), sort=g.get("sort") or None,
            direction=g.get("dir", "asc"), filters=filters, include_inactive=g.get("inactive") == "1",
            schema=schema_param(request)))
    except (ValueError, TypeError):
        return error("offset, limit and filters are not valid")
    except rows_mod.RowsError as exc:
        return _rows_error(exc)


@endpoint("GET")
@unknown_schema_is_400
def table_options(request, name):
    try:
        return JsonResponse(rows_mod.options(name, q=request.GET.get("q", ""), limit=int(request.GET.get("limit", 100)),
                                                 schema=schema_param(request)))
    except ValueError:
        return error("limit is not valid")
    except rows_mod.RowsError as exc:
        return _rows_error(exc)


@endpoint("GET")
@unknown_schema_is_400
def table_export_csv(request, name):
    """The whole filtered, sorted view of one table as CSV (editor role). Capped by limits.maxExportRows."""
    if not ROLE_CHECK["editor"](request.user):
        return error("exporting data needs the editor role", 403)
    g = request.GET
    schema = schema_param(request)
    try:
        filters = json.loads(g.get("filters", "{}") or "{}")
        if not isinstance(filters, dict):
            raise ValueError
    except (ValueError, TypeError):
        return error("filters are not valid")
    try:
        ex = rows_mod.open_csv(name, sort=g.get("sort") or None, direction=g.get("dir", "asc"), filters=filters,
                               include_inactive=g.get("inactive") == "1", schema=schema,
                               cap=get_config()["limits"]["maxExportRows"])
    except rows_mod.RowsError as exc:
        return _rows_error(exc)
    try:
        record(request.user, "table.export_csv", name,
               {"table": name, "schema": schema, "rows": ex.total, "filters": filters,
                "sort": g.get("sort") or None, "dir": g.get("dir", "asc"), "inactive": g.get("inactive") == "1"})
        chunks = ex.async_chunks() if hasattr(request, "scope") else ex.chunks()
        resp = StreamingHttpResponse(chunks, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{ex.filename()}"'
        resp["Cache-Control"] = "no-store"
    except BaseException:
        ex.close()
        raise
    return resp
