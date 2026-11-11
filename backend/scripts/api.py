"""REST endpoints for script intake (plan task 10). Editor-only manage, owner-or-approver read."""
from django.http import JsonResponse

from accounts.api import endpoint, error
from schema.http import schema_param, unknown_schema_is_400

from . import jobs, service, storage, uploads
from .models import Script


def _err(exc: storage.ScriptError) -> JsonResponse:
    return error(str(exc), 400)


def _summary(s: Script) -> dict:
    return {
        "id": s.pk, "name": s.name, "sha256": s.sha256, "size": s.size,
        "status": s.status, "change_id": s.change_id,
        "created_at": s.created_at.isoformat(), "expires_at": s.expires_at.isoformat(),
        "report": (jobs.STOPPED if jobs.is_stale(s.report) else s.report) or None,
    }


def _multipart(fn):
    """endpoint()-like guard for multipart POSTs (no json body check) plus GET pass-through."""
    def wrapper(request, *args, **kwargs):
        if request.method not in ("GET", "POST"):
            return error("method not allowed", 405)
        if not request.user.is_authenticated:
            return error("unauthenticated", 401)
        return fn(request, *args, **kwargs)
    return wrapper


@_multipart
def scripts(request):
    """GET: list own scripts (approvers see all). POST (multipart file, editor): upload."""
    if request.method == "POST":
        if not request.user.can_edit:
            return error("forbidden", 403)
        # JSON paste path: {"name": "...", "content": "..."} — same caps as file upload
        if request.content_type == "application/json":
            import json as _json
            try:
                body = _json.loads(request.body)
            except ValueError:
                return error(f"invalid JSON body (got {request.body[:80]!r})")
            name = (body.get("name") or "pasted.sql").strip()
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                return error("JSON body needs non-empty 'content' (the SQL text)")
            try:
                script = service.upload(request.user, name, content.encode("utf-8"))
            except storage.ScriptError as exc:
                return error(str(exc), 413 if "larger than" in str(exc) else 400)
            return JsonResponse({"script_id": script.pk, "sha256": script.sha256, "size": script.size}, status=201)
        f = request.FILES.get("file")
        if f is None:
            return error("multipart field 'file' is required")
        try:
            script = service.upload(request.user, f.name, f.read())
        except storage.ScriptError as exc:
            return error(str(exc), 413 if "larger than" in str(exc) else 400)
        return JsonResponse({"script_id": script.pk, "sha256": script.sha256, "size": script.size}, status=201)
    qs = Script.objects.all()
    if not request.user.can_approve:
        qs = qs.filter(owner=request.user)
    return JsonResponse({"scripts": [_summary(s) for s in qs[:50]]})


@endpoint("POST", min_role="editor")
@unknown_schema_is_400
def validate_view(request, script_id):
    if request.GET.get("async"):
        try:
            return JsonResponse(jobs.start(request.user, script_id, request.json.get("mapping"),
                                           schema_param(request, request.json)), status=202)
        except jobs.Forbidden as exc:
            return error(str(exc), 403)
        except jobs.Busy as exc:
            return error(str(exc), 409)
        except storage.ScriptError as exc:
            return _err(exc)
    try:
        report = service.validate(request.user, script_id, request.json.get("mapping"),
                                  schema_param(request, request.json))
    except PermissionError as exc:
        return error(str(exc), 403)
    except storage.ScriptError as exc:
        return _err(exc)
    return JsonResponse(report)


@endpoint("POST", min_role="editor")
def submit_view(request, script_id):
    try:
        change = service.submit(request.user, script_id, str(request.json.get("note", ""))[:2000])
    except storage.ScriptError as exc:
        return error(str(exc), 409)
    return JsonResponse({"change_id": change.pk}, status=201)


@endpoint("GET")
def script_detail(request, script_id):
    s = Script.objects.filter(pk=script_id).first()
    if s is None:
        return error("not found", 404)
    if s.owner_id != request.user.pk and not request.user.can_approve:
        return error("forbidden", 403)
    return JsonResponse(_summary(s))


def _upload_view(methods, fn):
    """Editor-only guard for the chunked-upload views: no JSON parsing of the body (chunks are raw bytes)."""
    def wrapper(request, *args, **kwargs):
        if request.method not in methods:
            return error("method not allowed", 405)
        if not request.user.is_authenticated:
            return error("unauthenticated", 401)
        if not request.user.can_edit:
            return error("forbidden", 403)
        try:
            return fn(request, *args, **kwargs)
        except uploads.NotFound as exc:
            return error(str(exc), 404)
        except uploads.Gone as exc:
            return error(str(exc), 410)
        except storage.ScriptError as exc:
            return error(str(exc), 413 if "larger than" in str(exc) else 400)
    return wrapper


def _json_body(request) -> dict:
    import json as _json
    try:
        body = _json.loads(request.body or b"{}")
    except ValueError:
        raise storage.ScriptError("invalid JSON body") from None
    if not isinstance(body, dict):
        raise storage.ScriptError("body must be a JSON object")
    return body


def upload_begin(request):
    def run(request):
        body = _json_body(request)
        return JsonResponse(uploads.begin(request.user, str(body.get("name") or "script.sql"), body.get("size")),
                            status=201)
    return _upload_view({"POST"}, run)(request)


def upload_chunk(request, upload_id, n):
    def run(request):
        # request.read(), not request.body: body would refuse anything over DATA_UPLOAD_MAX_MEMORY_SIZE
        return JsonResponse(uploads.put_chunk(request.user, upload_id, n, request.read()))
    return _upload_view({"PUT"}, run)(request)


def upload_status(request, upload_id):
    return _upload_view({"GET"}, lambda r: JsonResponse(uploads.status(r.user, upload_id)))(request)


def upload_complete(request, upload_id):
    def run(request):
        script = uploads.complete(request.user, upload_id)
        return JsonResponse({"script_id": script.pk, "sha256": script.sha256, "size": script.size}, status=201)
    return _upload_view({"POST"}, run)(request)
