"""The app must not start in production with a guessable SECRET_KEY."""
import os
import subprocess
import sys

import pytest

CODE = "import config.settings as s; print(s.SECRET_KEY)"


def run(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    return subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, env={**base, **env})


@pytest.mark.parametrize("key", [None, "", "change-me", "dev-insecure-key"])
def test_production_refuses_a_missing_or_placeholder_key(key):
    r = run(**({} if key is None else {"DJANGO_SECRET_KEY": key}))
    assert r.returncode != 0 and "DJANGO_SECRET_KEY" in r.stderr


def test_production_accepts_a_real_key():
    r = run(DJANGO_SECRET_KEY="x" * 50)
    assert r.returncode == 0 and r.stdout.strip() == "x" * 50


def test_debug_may_use_the_dev_key():
    assert run(DJANGO_DEBUG="1").returncode == 0
