from django.db.models import Count, Q
from django.http import JsonResponse

from accounts.api import endpoint, error
from approvals import service as changes
from audit.models import record
from schema import introspect
from schema.http import unknown_schema_is_400

from . import service
from .models import DuplicateGroup


def _fail(exc):
    return error(str(exc), exc.status)


def _group(pk):
    return DuplicateGroup.objects.select_related("merge_change").filter(pk=pk).first()


@endpoint("GET")
@unknown_schema_is_400
def groups(request):
    qs = DuplicateGroup.objects.select_related("merge_change")
    if schema := request.GET.get("schema"):
        qs = qs.filter(schema=introspect.require_managed(schema))
    if table := request.GET.get("table"):
        qs = qs.filter(table_name=table)
    items = [service.summary(g) for g in qs]
    if status := request.GET.get("status"):
        items = [g for g in items if g["status"] == status]
    open_groups = DuplicateGroup.objects.filter(status="open")
    if schema:
        open_groups = open_groups.filter(schema=schema)
    tables = open_groups.values("table_name").annotate(n=Count("id"))
    return JsonResponse({"groups": items, "tables": {t["table_name"]: t["n"] for t in tables}})


@endpoint("GET")
def group_detail(request, group_id):
    g = _group(group_id)
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
                              schema=request.GET.get("schema") or b.get("schema") or "master")
    except service.DuplicateError as exc:
        return _fail(exc)
    record(request.user, "duplicates.scanned", target=result["table"], details=result)
    return JsonResponse(result)


@endpoint("POST", min_role="editor")
def dismiss(request, group_id):
    g = _group(group_id)
    if g is None:
        return error("not found", 404)
    g.status, g.decided_by = DuplicateGroup.Status.DISMISSED, request.user
    g.save(update_fields=["status", "decided_by"])
    record(request.user, "duplicates.dismissed", target=f"#{g.pk}", details={"table": g.table_name})
    return JsonResponse(service.summary(g))


@endpoint("POST", min_role="editor")
def reopen(request, group_id):
    g = _group(group_id)
    if g is None:
        return error("not found", 404)
    g.status = DuplicateGroup.Status.OPEN
    g.save(update_fields=["status"])
    return JsonResponse(service.summary(g))


@endpoint("POST", min_role="editor")
def merge(request, group_id):
    g = _group(group_id)
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
