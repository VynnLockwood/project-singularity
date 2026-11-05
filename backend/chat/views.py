from django.http import JsonResponse

from accounts.api import endpoint, error
from approvals import service
from approvals.models import Change
from config.siconfig import get_config

from . import orchestrator
from .models import Conversation, Message
from .openrouter import ChatError

CHAT_STATUS = {"not_configured": 503, "unavailable": 502, "provider_error": 502, "rate_limited": 429}


def _conv(user, conv_id):
    return Conversation.objects.filter(pk=conv_id, user=user).first()


def _message(m):
    out = {"id": m.pk, "role": m.role, "content": m.content, "createdAt": m.created_at.isoformat()}
    if m.role == Message.Role.ASSISTANT and m.tool_calls:
        out["toolCalls"] = [c["name"] for c in m.tool_calls]
    if m.role == Message.Role.TOOL:
        out["toolName"] = m.tool_name
    return out


def _changes(conv):
    qs = Change.objects.filter(conversation_id=conv.pk, requester=conv.user).select_related("requester", "decided_by")
    return [service.serialize(c) for c in qs.order_by("id")]


def _summary(c):
    return {"id": c.pk, "title": c.title, "model": c.model, "updatedAt": c.updated_at.isoformat()}


@endpoint("GET")
def models(request):
    cfg = get_config()["chat"]
    return JsonResponse({"default": cfg["defaultModel"], "allowed": cfg["allowedModels"]})


@endpoint(["GET", "POST"])
def conversations(request):
    if request.method == "GET":
        items = Conversation.objects.filter(user=request.user)[:100]
        return JsonResponse({"conversations": [_summary(c) for c in items]})
    cfg = get_config()["chat"]
    model = request.json.get("model") or cfg["defaultModel"]
    allowed = set(cfg["allowedModels"]) | ({cfg["defaultModel"]} if cfg["defaultModel"] else set())
    if not model or model not in allowed:
        return error("model is not allowed", 400)
    conv = Conversation.objects.create(user=request.user, model=model)
    return JsonResponse(_summary(conv), status=201)


@endpoint("GET")
def conversation_detail(request, conv_id):
    conv = _conv(request.user, conv_id)
    if conv is None:
        return error("not found", 404)
    return JsonResponse({**_summary(conv), "messages": [_message(m) for m in conv.messages.all()],
                         "changes": _changes(conv)})


@endpoint("POST")
def send_message(request, conv_id):
    conv = _conv(request.user, conv_id)
    if conv is None:
        return error("not found", 404)
    text = request.json.get("content")
    if not isinstance(text, str) or not text.strip():
        return error("content is required", 400)
    if len(text) > 20_000:
        return error("message is too long", 400)
    try:
        added = orchestrator.run_turn(conv, request.user, text.strip())
    except ChatError as exc:
        return error(exc.message, CHAT_STATUS.get(exc.code, 502), code=exc.code)
    return JsonResponse({"messages": [_message(m) for m in added], "changes": _changes(conv)})
