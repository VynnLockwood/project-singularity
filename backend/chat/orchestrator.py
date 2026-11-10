"""One chat turn: call the model, run its tool calls in-process as the signed-in user, repeat."""
import json
import logging
from datetime import timedelta

from django.utils import timezone

from config.siconfig import get_config
from mcp_server import tools as mcp_tools

from . import providers
from .errors import ChatError
from .models import Message
from .toolspecs import specs_for

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are Singularity's assistant. You help a team design and manage a master-data PostgreSQL database.
A "database" is a Postgres schema (default master); call list_schemas to see them and pass `schema` to the other tools.
Start by calling get_rules and list_tables when you need to know the current rules or structure. Before adding a column or table, call search_schema to see whether the data already has a home.
You never write SQL to change anything: call propose_change with structured operations. The gate checks
the proposal against the team's rules and returns a diff. The user then sees an Approve / Reject popup,
so tell them what you proposed and that it is waiting for their decision. Never say a change is applied
until get_change shows status 'applied'. Settings, gate rules and roles are changed with propose_config_change (call get_settings first); they always wait for an approver. Schema changes by editors wait for an approver. Dropping things
needs a 'reason'. Use query for read-only SELECTs. Table and column names are English; values can be Thai.
Reply in the language the user writes in. Be brief.
"""

MAX_TOOL_OUTPUT = 20_000
# One tool result must fit in MAX_TOOL_OUTPUT once it is JSON, so in chat the big readers return less
# than they do for an MCP client: a short page of rows, and an export cut well under the limit.
CHAT_PAGE = 25
CHAT_EXPORT_BYTES = 12_000


def check_rate_limit(user) -> None:
    limit = get_config()["chat"]["messagesPerMinute"]
    since = timezone.now() - timedelta(minutes=1)
    recent = Message.objects.filter(conversation__user=user, role=Message.Role.USER, created_at__gte=since).count()
    if recent >= limit:
        raise ChatError("rate_limited", "too many messages; wait a moment and try again")


def history(conv) -> list[dict]:
    out = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in conv.messages.all():
        if m.role == Message.Role.USER:
            out.append({"role": "user", "content": m.content})
        elif m.role == Message.Role.ASSISTANT:
            msg = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in m.tool_calls
                ]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
    return out


def run_tool(user, conv, name: str, raw_args: str) -> dict:
    allowed = {s["function"]["name"] for s in specs_for(user)}
    if name not in allowed:
        return {"error": f"tool {name!r} is not available"}
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return {"error": "tool arguments must be a JSON object"}
    try:
        if name == "propose_change":
            result = mcp_tools.propose_change(user, **args, source="web", conversation_id=conv.pk)
            if result.get("status") == "pending":
                result["next"] = (
                    "The user sees an Approve / Reject popup for this change. Summarise the diff and say it is "
                    f"waiting for their decision (change #{result['id']}). Do not say it is applied."
                )
            return result
        if name == "propose_config_change":
            result = mcp_tools.propose_config_change(user, **args, source="web", conversation_id=conv.pk)
            if result.get("status") == "pending":
                who = "you see an Approve / Reject popup" if user.can_approve else "an approver will see it live in their approval queue"
                result["next"] = (
                    f"This is a settings, rules or roles change, so it always waits for an approver: {who} (change #{result['id']}). "
                    "Summarise the diff and say it is waiting. Do not say it is applied."
                )
            return result
        if name == "merge_duplicates":
            result = mcp_tools.merge_duplicates(user, **args, source="web", conversation_id=conv.pk)
            if result["change"].get("status") == "pending":
                result["change"]["next"] = (
                    "The user sees this merge in the review tray and decides it there. Say which record is kept and "
                    f"that it is waiting for their decision (change #{result['change']['id']}). Do not say it is applied."
                )
            return result
        if name == "export_schema":
            return mcp_tools.export_schema(user, **args, max_bytes=CHAT_EXPORT_BYTES)
        if name == "read_rows":
            asked = args.get("limit")
            args["limit"] = min(asked, CHAT_PAGE) if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 else CHAT_PAGE
        return mcp_tools.IMPL[name](user, **args)
    except mcp_tools.ToolFailure as exc:
        return {"error": str(exc)}
    except TypeError:
        return {"error": "invalid tool arguments"}
    except Exception:  # never leak internals to the model or the user
        log.exception("chat tool %s failed", name)
        return {"error": "the tool failed"}


def run_turn(conv, user, text: str, api_key: str = "") -> list[Message]:
    """Store the user's message, drive the model to a final answer, return every message added.

    `api_key` is a per-request, caller-supplied key. It is used only for this turn and is
    never written to the database or a log.
    """
    check_rate_limit(user)
    if not conv.model:
        raise ChatError("not_configured", "no chat model is configured")
    added = [Message.objects.create(conversation=conv, role=Message.Role.USER, content=text)]
    if not conv.title:
        conv.title = text.strip().splitlines()[0][:80] if text.strip() else "Chat"
    conv.save()

    rounds = get_config()["chat"]["maxToolRounds"]
    specs = specs_for(user)
    for _ in range(rounds):
        reply = providers.complete(get_config()["chat"]["provider"], history(conv), specs, conv.model,
                                   key=api_key)
        added.append(Message.objects.create(
            conversation=conv, role=Message.Role.ASSISTANT, content=reply["content"], tool_calls=reply["tool_calls"]))
        if not reply["tool_calls"]:
            return added
        for call in reply["tool_calls"]:
            result = run_tool(user, conv, call["name"], call["arguments"])
            body = json.dumps(result, default=str, ensure_ascii=False)
            if len(body) > MAX_TOOL_OUTPUT:
                body = json.dumps({"error": "result too large; narrow the request"})
            added.append(Message.objects.create(
                conversation=conv, role=Message.Role.TOOL, content=body,
                tool_call_id=call["id"], tool_name=call["name"]))
    added.append(Message.objects.create(
        conversation=conv, role=Message.Role.ASSISTANT,
        content="I stopped after too many steps. Tell me how you would like to continue."))
    return added
