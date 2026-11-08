"""The quality knobs approvers can change at runtime (everything else stays in singularity.yaml)."""
import os
from audit.models import record
from config.siconfig import _yaml_config, get_config, invalidate_overrides

from .models import Setting

# provider -> the environment variable holding its API key. The key value itself never leaves the server:
# only `bool(env)` is ever read out (see key_set).
PROVIDER_KEYS = {"openrouter": "OPENROUTER_API_KEY", "opencode": "OPENCODE_API_KEY"}

# key -> (label, help, kind, min, max)
TUNABLES = {
    "search.minConfidence": ("Search confidence", "A schema search match counts as 'confident' at or above this score (0 to 1). Higher means fewer, surer matches.", "float", 0.0, 1.0),
    "search.topK": ("Search results", "How many tables and columns a schema search returns.", "int", 1, 20),
    "duplicates.threshold": ("Duplicate similarity", "Records scoring at or above this are grouped as possible duplicates (0.5 to 1). Higher finds fewer, surer groups.", "float", 0.5, 1.0),
    "duplicates.maxRows": ("Duplicate scan row cap", "A scan reads at most this many rows of a table.", "int", 100, 100000),
    "limits.maxOpsPerProposal": ("Operations per proposal", "The most structured operations one proposed change may contain.", "int", 1, 200),
    "limits.maxRowsPerDataOp": ("Rows per data operation", "The most rows one insert, update or deactivate may touch.", "int", 1, 10000),
    "limits.maxQueryRows": ("Query row limit", "Read-only queries return at most this many rows.", "int", 10, 5000),
    "limits.queryTimeoutMs": ("Query timeout (ms)", "A read-only query is stopped after this long.", "int", 500, 60000),
    "limits.maxScriptBytes": ("Script file size (bytes)", "The largest SQL script file that may be uploaded for intake.", "int", 1024, 209715200),
    "limits.maxScriptRows": ("Script rows", "The most rows one script intake may contain.", "int", 1, 2000000),
    "limits.maxOpsPerScript": ("Operations per script", "The most operations one validated script may produce.", "int", 1, 100000),
    "limits.maxInlineScriptBytes": ("Inline script size (bytes)", "The largest script that may be pasted inline rather than uploaded.", "int", 1024, 1048576),
    "chat.provider": ("Chat provider", "Which service answers chats. Its API key comes from the server environment and is never shown here.", "choice", None, None),
    "chat.defaultModel": ("Default chat model", "The model new chats start with. Choose from the models allowed in singularity.yaml.", "model", None, None),
    "chat.maxToolRounds": ("Tool rounds per message", "How many tool calls the assistant may chain for one message.", "int", 1, 20),
    "chat.messagesPerMinute": ("Messages per minute", "Chat rate limit per person.", "int", 1, 120),
    "backups.retentionDays": ("Backup retention (days)", "How long schema-change backups are kept.", "int", 1, 365),
}

# kind -> allowed values (anything else must be a number)
OPTIONS = {"model": lambda: _yaml_config()["chat"]["allowedModels"],
           "choice": lambda: list(PROVIDER_KEYS)}


class TunableError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _default(key):
    section, name = key.split(".")
    return _yaml_config()[section][name]


def key_set() -> dict:
    """Which providers have an API key configured, as booleans only (the value stays in the environment)."""
    return {provider: bool(os.environ.get(env, "")) for provider, env in PROVIDER_KEYS.items()}


def describe() -> list[dict]:
    cfg = get_config()
    overridden = set(Setting.objects.values_list("key", flat=True))
    out = []
    for key, (label, help_, kind, lo, hi) in TUNABLES.items():
        section, name = key.split(".")
        out.append({
            "key": key, "label": label, "help": help_, "kind": kind, "min": lo, "max": hi,
            "default": _default(key), "value": cfg[section][name], "overridden": key in overridden,
            **({"options": OPTIONS[kind]()} if kind in OPTIONS else {}),
        })
    return out


def _clean(key, value):
    label, _, kind, lo, hi = TUNABLES[key]
    if kind in OPTIONS:
        if value not in OPTIONS[kind]():
            raise ValueError(f"{label}: choose one of the allowed {kind}s")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: must be a number")
    if kind == "int":
        if value != int(value):
            raise ValueError(f"{label}: must be a whole number")
        value = int(value)
    else:
        value = float(value)
    if not (lo <= value <= hi):
        raise ValueError(f"{label}: must be between {lo} and {hi}")
    return value


def apply(actor, changes: dict) -> list[dict]:
    """Set (value) or reset to the file's default (null) one or more tunables. All or nothing."""
    if not actor.can_approve:
        raise TunableError(["only approvers can change settings"])
    if not isinstance(changes, dict) or not changes:
        raise TunableError(["nothing to change"])
    errors, clean = [], {}
    for key, value in changes.items():
        if key not in TUNABLES:
            errors.append(f"unknown setting {key!r}")
            continue
        if value is None:
            clean[key] = None
            continue
        try:
            clean[key] = _clean(key, value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise TunableError(errors)
    before = get_config()
    for key, value in clean.items():
        if value is None:
            Setting.objects.filter(key=key).delete()
        else:
            Setting.objects.update_or_create(key=key, defaults={"value": value, "updated_by": actor})
    invalidate_overrides()
    after = get_config()
    diff = {k: {"from": before[k.split(".")[0]][k.split(".")[1]], "to": after[k.split(".")[0]][k.split(".")[1]]}
            for k in clean if before[k.split(".")[0]][k.split(".")[1]] != after[k.split(".")[0]][k.split(".")[1]]}
    if diff:
        record(actor, "settings.updated", target=",".join(sorted(diff)), details=diff)
    return describe()
