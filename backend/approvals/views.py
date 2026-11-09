from django.http import JsonResponse

from accounts.api import endpoint, error, limit_param

from . import service
from .models import Change

STATUS = {"forbidden": 403, "not_found": 404, "bad_state": 409, "invalid": 400}


def _err(exc: service.ChangeError):
    return error(exc.message, STATUS.get(exc.code, 400), code=exc.code)


@endpoint(["GET", "POST"])
def changes(request):
    if request.method == "GET":
        qs = Change.objects.select_related("requester", "decided_by")
        if status := request.GET.get("status"):
            qs = qs.filter(status=status)
        if schema := request.GET.get("schema"):
            qs = qs.filter(schema=schema)
        if kind := request.GET.get("kind"):
            qs = qs.filter(kind=kind)
        if request.GET.get("mine") == "1":
            qs = qs.filter(requester=request.user)
        return JsonResponse({"changes": [service.serialize(c, detail=False) for c in qs[: limit_param(request)]]})

    if not request.user.can_edit:
        return error("forbidden", 403)
    body = request.json
    source = body.get("source", "web")
    if source not in ("web", "grid"):          # 'mcp' and 'revert' are set by the server, never by a caller
        return error("source must be 'web' or 'grid'")
    try:
        change = service.propose(
            request.user, body.get("operations"), source=source,
            prompt=str(body.get("prompt", ""))[:10_000], conversation_id=body.get("conversation_id"),
            schema=body.get("schema") or "master",
        )
    except service.ChangeError as exc:
        return _err(exc)
    return JsonResponse(service.serialize(change), status=201)


@endpoint("GET")
def change_detail(request, change_id):
    change = Change.objects.select_related("requester", "decided_by").filter(pk=change_id).first()
    if change is None:
        return error("not found", 404)
    return JsonResponse(service.serialize(change))


ACTIONS = {
    "approve": ("editor", lambda u, i, b: service.approve(u, i)),
    "reject": ("editor", lambda u, i, b: service.reject(u, i, str(b.get("note", ""))[:2000])),
    "recheck": ("editor", lambda u, i, b: service.recheck(u, i)),
    "revert": ("approver", lambda u, i, b: service.revert(u, i)),
}


@endpoint("POST")
def change_action(request, change_id, action):
    spec = ACTIONS.get(action)
    if spec is None:
        return error("not found", 404)
    need, run = spec
    allowed = request.user.can_approve if need == "approver" else request.user.can_edit
    if not allowed:
        return error("forbidden", 403)
    try:
        change = run(request.user, change_id, request.json)
    except service.ChangeError as exc:
        return _err(exc)
    return JsonResponse(service.serialize(change))


@endpoint("GET", min_role="editor")
def queue(request):
    items = service.pending_for(request.user).select_related("requester", "decided_by")
    return JsonResponse({"changes": [service.serialize(c) for c in items[: limit_param(request)]]})
