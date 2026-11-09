import json

from django.http import JsonResponse, StreamingHttpResponse

from accounts.api import ROLE_CHECK, endpoint, error
from audit.models import record
from config.siconfig import get_config

from . import introspect, query, rows as rows_mod
from .dump_model import ExportError
from .export import open_export
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
def export(request):
    """Download a schema as Postgres SQL: `?schema=master&data=0|1`. Data needs the editor role."""
    schema = request.GET.get("schema", "master")
    flag = request.GET.get("data", "0")
    if flag not in ("0", "1"):
        return error("data must be 0 or 1", 400)
    with_data = flag == "1"
    if with_data and not ROLE_CHECK["editor"](request.user):
        return error("exporting data needs the editor role", 403)
    try:
        ex = open_export(schema, data=with_data, cap=get_config()["limits"]["maxExportRows"])
    except ExportError as exc:
        return error(str(exc), exc.status)
    try:
        record(request.user, "schema.export", schema, {"schema": schema, "data": with_data, "counts": ex.counts})
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
            schema=request.GET.get("schema") or request.json.get("schema") or "master"))
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
