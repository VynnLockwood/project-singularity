from django.db.models import Count
from django.http import JsonResponse

from accounts.api import endpoint, error
from approvals import service as changes
from audit.models import record
from schema.http import schema_for, schema_param, unknown_schema_is_400

from . import service


def _fail(exc):
    return error(str(exc), exc.status)


def _group(request, pk, level="viewer"):
    """The group, or None when it does not exist or is in a database the user cannot reach (the same 404).
    A readable database where the user lacks `level` raises `access.AccessDenied` (403, see `unknown_schema_is_400`)."""
    try:
        return service.get_group(pk, request.user, level)
    except service.DuplicateError:
        return None


@endpoint("GET")
@unknown_schema_is_400
def groups(request):
    schema = schema_param(request, allow_all=True)  # absent = master like everywhere; explicit `*` = all schemas
    items = service.list_groups(schema=schema, table=request.GET.get("table"), status=request.GET.get("status"),
                                user=request.user)
    open_groups = service.visible_groups(request.user).filter(status="open")   # counts never include hidden ones
    if schema:
        open_groups = open_groups.filter(schema=schema)
    tables = open_groups.values("table_name").annotate(n=Count("id"))
    return JsonResponse({"groups": items, "tables": {t["table_name"]: t["n"] for t in tables}})


@endpoint("GET")
@unknown_schema_is_400
def group_detail(request, group_id):
    g = _group(request, group_id)
    if g is None:
        return error("not found", 404)
    try:
        return JsonResponse(service.detail(g))
    except Exception:  # the table may have been dropped since the scan
        return error("could not read the records of this group", 404)


@endpoint("POST", min_role="editor")
@unknown_schema_is_400
def scan(request):
    b = request.json
    fields = b.get("fields")
    if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)):
        return error("fields must be a list of column names")
    try:
        result = service.scan(str(b.get("table", "")), request.user, fields=fields, threshold=b.get("threshold"),
                              schema=schema_for(request, "editor", b))
    except service.DuplicateError as exc:
        return _fail(exc)
    record(request.user, "duplicates.scanned", target=result["table"], details=result)
    return JsonResponse(result)


@endpoint("POST", min_role="editor")
@unknown_schema_is_400
def dismiss(request, group_id):
    g = _group(request, group_id, "editor")
    if g is None:
        return error("not found", 404)
    return JsonResponse(service.dismiss(g, request.user))


@endpoint("POST", min_role="editor")
@unknown_schema_is_400
def reopen(request, group_id):
    g = _group(request, group_id, "editor")
    if g is None:
        return error("not found", 404)
    try:
        return JsonResponse(service.reopen(g, request.user))
    except service.DuplicateError as exc:
        return _fail(exc)


@endpoint("POST", min_role="editor")
@unknown_schema_is_400
def merge(request, group_id):
    g = _group(request, group_id, "editor")
    if g is None:
        return error("not found", 404)
    survivor = request.json.get("survivorId")
    if not isinstance(survivor, int) or isinstance(survivor, bool):
        return error("survivorId is required")
    try:
        change = service.propose_merge(g, survivor, request.user)
    except service.DuplicateError as exc:
        return _fail(exc)
    return JsonResponse({"group": service.summary(g), "change": changes.serialize(change)}, status=201)
