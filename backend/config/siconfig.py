"""Deploy-time config loader for singularity.yaml (spec section 6)."""
import copy
import os
import time
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULTS = {
    "auth": {
        "provider": "google",
        "allowedDomains": [],
        "seedAdmins": [],
        "defaultRole": "viewer",
    },
    "limits": {
        "maxQueryRows": 500,
        "queryTimeoutMs": 5000,
        "maxOpsPerProposal": 50,
        "maxRowsPerDataOp": 1000,
        "maxScriptBytes": 52428800,
        "maxScriptRows": 200000,
        "maxOpsPerScript": 1000,
        "maxInlineScriptBytes": 20480,
    },
    "embedding": {"model": "intfloat/multilingual-e5-small", "dimension": 384},
    "chat": {"provider": "openrouter", "defaultModel": "", "allowedModels": [], "models": [], "maxToolRounds": 8,
             "messagesPerMinute": 20},
    "search": {"topK": 5, "minConfidence": 0.8, "sampleValues": 5},
    "duplicates": {"threshold": 0.9, "maxRows": 20000},
    "backups": {"retentionDays": 14},
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _path() -> Path:
    env = os.environ.get("SINGULARITY_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "singularity.yaml"


def seed_models(chat: dict) -> list:
    """The file's seed for the site-managed model list: allowedModels plus defaultModel when it is missing."""
    models = list(chat.get("allowedModels") or [])
    default = chat.get("defaultModel")
    if default and default not in models:
        models.append(default)
    return models


@lru_cache(maxsize=1)
def _yaml_config() -> dict:
    path = _path()
    loaded = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
    cfg = _merge(DEFAULTS, loaded)
    cfg["auth"]["allowedDomains"] = [d.lower() for d in cfg["auth"]["allowedDomains"]]
    cfg["auth"]["seedAdmins"] = [e.lower() for e in cfg["auth"]["seedAdmins"]]
    cfg["chat"]["models"] = seed_models(cfg["chat"])  # seed for the site-managed list
    return cfg


# ---- runtime overrides ---------------------------------------------------------
# singularity.yaml is deploy-time. A short list of quality knobs can also be changed by approvers in the
# Settings tab; those values live in the database and are merged over the file (see settings_app.tunables).
# The chat provider and model list are also managed there (yaml allowedModels/defaultModel only seed the first
# value). Everything else (auth, embedding) stays deploy-time on purpose.

_OVERRIDE_TTL = 5.0  # seconds: other worker processes notice a change within this time
_override_cache: dict = {"at": 0.0, "data": {}}


def overrides() -> dict:
    now = time.monotonic()
    if now - _override_cache["at"] > _OVERRIDE_TTL:
        try:
            from settings_app.models import Setting

            data = {row.key: row.value for row in Setting.objects.all()}
        except Exception:  # no database yet (startup, migration), or called from async code: use the file
            data = {}
        _override_cache.update(at=now, data=data)
    return _override_cache["data"]


def invalidate_overrides() -> None:
    _override_cache["at"] = 0.0


def get_config() -> dict:
    """The file's config with the approvers' runtime overrides applied (a fresh copy each time)."""
    cfg = copy.deepcopy(_yaml_config())
    cfg["chat"]["models"] = seed_models(cfg["chat"])  # seed until chat.models is overridden
    for key, value in overrides().items():
        section, _, name = key.partition(".")
        if name and isinstance(cfg.get(section), dict):
            cfg[section][name] = copy.deepcopy(value)
    return cfg


def _clear_all() -> None:
    _yaml_config.cache_clear()
    invalidate_overrides()


get_config.cache_clear = _clear_all  # type: ignore[attr-defined]  # tests and reloads use this
