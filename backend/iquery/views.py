from django.http import HttpResponse, JsonResponse

from accounts.api import endpoint, error
from schema import introspect
from schema.http import schema_param, unknown_schema_is_400

from . import memory, suggest
from .models import QueryExample

STATUS = {"rate_limited": 429, "not_configured": 503, "provider_error": 502, "bad_request": 400}


def _inputs(request):
    """(schema, comments, sql_context, api_key) from the JSON body; SuggestError(bad_request) when wrong."""
    body = request.json
    schema = schema_param(request, body)
    introspect.require_managed(schema)
    comments, ctx, key = suggest.clean_inputs(body.get("comments"), body.get("sqlContext", ""), body.get("apiKey", ""))
    return schema, comments, ctx, key


def _run(request, fn):
    try:
        schema, comments, ctx, key = _inputs(request)
        out = fn(schema, comments, ctx, key)
    except suggest.SuggestError as exc:
        return error(exc.message, STATUS[exc.code], code=exc.code, requestId=request.json.get("requestId"))
    return JsonResponse({**out, "requestId": request.json.get("requestId")})


@endpoint("POST")
@unknown_schema_is_400
def recall(request):
    return _run(request, lambda schema, comments, ctx, key: suggest.recall_for(schema, comments))


@endpoint("POST")
@unknown_schema_is_400
def suggest_view(request):
    return _run(request, lambda schema, comments, ctx, key: suggest.suggest(request.user, schema, comments, ctx, key))


@endpoint("POST")
@unknown_schema_is_400
def learn(request):
    try:
        schema, comments, _, _ = _inputs(request)
    except suggest.SuggestError as exc:
        return error(exc.message, 400)
    sql, values = request.json.get("sql"), request.json.get("values", [])
    if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
        return error("values must be a list of objects")
    try:
        ex = memory.learn(request.user, schema, comments, sql, values)
    except ValueError as exc:
        return error(str(exc))
    return JsonResponse({"id": ex.pk, "status": ex.status})


@endpoint("GET", min_role="approver")
@unknown_schema_is_400
def examples(request):
    schema = schema_param(request)
    rows = QueryExample.objects.filter(schema=schema).select_related("created_by").order_by("-last_used", "-created_at")[:200]
    return JsonResponse({"examples": [{
        "id": e.pk, "comments": e.comments, "sql": e.sql, "status": e.status, "useCount": e.use_count,
        "createdBy": e.created_by.email if e.created_by else None,
        "lastUsed": e.last_used.isoformat() if e.last_used else None} for e in rows]})


@endpoint("DELETE", min_role="approver")
def example_detail(request, example_id):
    deleted, _ = QueryExample.objects.filter(pk=example_id).delete()
    return HttpResponse(status=204) if deleted else error("not found", 404)
