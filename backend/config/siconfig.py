"""Deploy-time config loader for singularity.yaml (spec section 6)."""
import os
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
    },
    "embedding": {"model": "intfloat/multilingual-e5-small", "dimension": 384},
    "chat": {"provider": "openrouter", "defaultModel": "", "allowedModels": [], "maxToolRounds": 8,
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


@lru_cache(maxsize=1)
def get_config() -> dict:
    path = _path()
    loaded = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
    cfg = _merge(DEFAULTS, loaded)
    cfg["auth"]["allowedDomains"] = [d.lower() for d in cfg["auth"]["allowedDomains"]]
    cfg["auth"]["seedAdmins"] = [e.lower() for e in cfg["auth"]["seedAdmins"]]
    return cfg
