from django.http import JsonResponse

from accounts.api import endpoint, error
from config.siconfig import get_config

from . import introspect, query


@endpoint("GET")
def tables(request):
    return JsonResponse({"tables": introspect.list_tables()})


@endpoint("GET")
def graph(request):
    return JsonResponse(introspect.graph())


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
