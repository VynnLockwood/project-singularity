"""HTTP endpoints for the Import tab: thin wrappers over intake.run (owner or approver only)."""
from django.http import HttpResponse, JsonResponse

from accounts.api import endpoint, error
from schema.http import schema_param, unknown_schema_is_400

from . import run as runs

MAX_PROMPT, MAX_ANSWER, MAX_KEY, MAX_NOTE = 2000, 1000, 500, 2000
STATUS = {"forbidden": 404, "not_found": 404, "bad_state": 409, "busy": 409, "invalid": 400, "access_denied": 403}
# Another person's run is reported as 404, like a run that does not exist: its id says nothing to anyone else.


def _fail(exc: runs.RunError, run_id: int | None = None):
    if exc.code in ("forbidden", "not_found") and run_id is not None:
        return error(f"import #{run_id} does not exist", 404, code="not_found")    # the same reply for both
    return error(exc.message, STATUS.get(exc.code, 400), code=exc.code, **exc.extra)   # busy: runId, schema


def _text(body: dict, key: str, limit: int, required: bool = False):
    """(value, None) or (None, error response): an optional string of at most `limit` characters."""
    v = body.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        return None, error(f"{key} must be text")
    if len(v) > limit:
        return None, error(f"{key} must be at most {limit:,} characters")
    if required and not v.strip():
        return None, error(f"{key} is required")
    return v, None


def _run_json(run) -> dict:
    return {
        "id": run.pk, "schema": run.schema, "state": run.state, "prompt": run.prompt, "fileName": run.file_name,
        "round": run.round, "profile": run.profile, "matches": run.matches, "plan": run.plan, "report": run.report,
        "messages": [{"role": m.role, "text": m.text, "details": m.details, "at": m.created_at.isoformat()}
                     for m in run.messages.order_by("id")],
        "changeId": run.change_id, "expiresAt": run.expires_at.isoformat(),
    }


def _reply(user, run_id: int, status: int = 200):
    return JsonResponse(_run_json(runs.get(user, run_id)), status=status)


@endpoint("POST", min_role="editor")
@unknown_schema_is_400
def imports(request):
    body = request.json
    schema = schema_param(request, body)
    upload_id = body.get("uploadId")
    if not isinstance(upload_id, int) or isinstance(upload_id, bool):
        return error("uploadId must be a number")
    prompt, bad = _text(body, "prompt", MAX_PROMPT)
    if bad:
        return bad
    key, bad = _text(body, "apiKey", MAX_KEY)
    if bad:
        return bad
    try:
        run = runs.start(request.user, upload_id, schema, prompt, api_key=key)
        return _reply(request.user, run.pk, 201)
    except runs.RunError as exc:
        return _fail(exc)


@endpoint(["GET", "DELETE"], min_role="editor")
def import_detail(request, run_id):
    try:
        if request.method == "DELETE":
            runs.cancel(request.user, run_id)
            return HttpResponse(status=204)
        return _reply(request.user, run_id)
    except runs.RunError as exc:
        return _fail(exc, run_id)


@endpoint("POST", min_role="editor")
def import_answer(request, run_id):
    text, bad = _text(request.json, "text", MAX_ANSWER, required=True)
    if bad:
        return bad
    key, bad = _text(request.json, "apiKey", MAX_KEY)
    if bad:
        return bad
    try:
        runs.answer(request.user, run_id, text, api_key=key)
        return _reply(request.user, run_id)
    except runs.RunError as exc:
        return _fail(exc, run_id)


@endpoint("POST", min_role="editor")
def import_recheck(request, run_id):
    body = request.json
    mapping, filters, skip = body.get("mapping"), body.get("filters"), body.get("skip")
    if not isinstance(mapping, dict):
        return error("mapping must be an object")
    if filters is not None and not isinstance(filters, dict):
        return error("filters must be an object")
    if skip is not None and not isinstance(skip, dict):
        return error("skip must be an object of sheet -> row numbers")
    try:
        runs.recheck(request.user, run_id, mapping, filters, skip)
        return _reply(request.user, run_id)
    except runs.RunError as exc:
        return _fail(exc, run_id)


@endpoint("POST", min_role="editor")
def import_submit(request, run_id):
    note, bad = _text(request.json, "note", MAX_NOTE)
    if bad:
        return bad
    try:
        change = runs.submit(request.user, run_id, note)
    except runs.RunError as exc:
        return _fail(exc, run_id)
    return JsonResponse({"changeId": change.pk}, status=201)
