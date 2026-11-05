#!/usr/bin/env python3
"""Static safety checks on docker-compose.yml (spec sections 1 and 7).

  - nginx is the only service that publishes host ports
  - db and redis are on the internal (no-internet) network only
  - the AI-facing services never get master credentials they don't need

Run: python3 scripts/check-compose.py   (needs the docker CLI; no daemon required)
"""
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
env = root / ".env.example"
out = subprocess.run(
    ["docker", "compose", "--env-file", str(env), "config", "--format", "json"],
    cwd=root, capture_output=True, text=True, check=True,
).stdout
services = json.loads(out)["services"]
errors = []

for name, svc in services.items():
    if svc.get("ports") and name != "nginx":
        errors.append(f"{name} publishes host ports; only nginx may")

nginx_ports = {int(p["target"]) for p in services["nginx"].get("ports", [])}
if nginx_ports != {80, 443}:
    errors.append(f"nginx should publish exactly 80 and 443, got {sorted(nginx_ports)}")

for name in ("db", "redis", "web", "embed"):
    nets = set(services[name].get("networks", {}))
    if nets != {"internal"}:
        errors.append(f"{name} must be on the internal network only, got {sorted(nets)}")

for name in ("db", "redis"):
    if "ports" in services[name]:
        errors.append(f"{name} must not publish ports")

for name in ("web", "embed", "db", "redis", "nginx"):
    for key in services[name].get("environment", {}):
        if key.startswith("GOOGLE_") or key.startswith("OPENROUTER_"):
            errors.append(f"{name} should not receive {key}")

for name in ("mcp", "worker"):
    if "OPENROUTER_API_KEY" in services[name].get("environment", {}):
        errors.append(f"{name} should not receive the OpenRouter key; only api calls the provider")

if errors:
    print("compose check FAILED:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("compose check OK: only nginx publishes ports; db/redis/web are internal-only")
