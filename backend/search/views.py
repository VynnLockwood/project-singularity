from django.http import JsonResponse

from accounts.api import endpoint, error
from audit.models import record
from schema.http import schema_param, unknown_schema_is_400

from . import indexer, service
from .embedder import SearchError


@endpoint("GET")
@unknown_schema_is_400
def search(request):
    try:
        top_k = int(request.GET.get("k", 0)) or None
    except ValueError:
        return error("k must be a number")
    try:
        # absent schema = master like everywhere; explicit `*` = all schemas
        return JsonResponse(service.search(request.GET.get("q", ""), top_k, schema_param(request, allow_all=True)))
    except SearchError as exc:
        return error(str(exc), 400 if "required" in str(exc) else 503)


@endpoint("POST", min_role="approver")
def reindex(request):
    try:
        result = indexer.reindex(rebuild=bool(request.json.get("rebuild")))
    except SearchError as exc:
        return error(str(exc), 503)
    record(request.user, "search.reindexed", details=result)
    return JsonResponse(result)
