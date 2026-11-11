"""A script that names its schema (master_thach.x) is checked against that database unless the person picks another."""
import json

import pytest
from django.test import Client

from accounts.models import Role, User
from mcp_server import tools
from scripts import storage
from scripts.validate import validate

from .test_executor_schemas import _cleanup_schemas, apply, col, make_crm  # noqa: F401  (the cleanup fixture is autouse)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

DUMP = "COPY crm.thing (code, name) FROM stdin;\nA1\tAlpha\nB2\tBeta\n\\.\n"
UNKNOWN = DUMP.replace("crm.", "nosuch.")
PLAIN = DUMP.replace("crm.", "")


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


@pytest.fixture
def owner():
    return User.objects.create(username="ed@x.io", email="ed@x.io", role=Role.EDITOR)


@pytest.fixture
def crm():
    make_crm()
    apply([{"op": "create_table", "name": "thing", "columns": [col("code"), col("name")]}], schema="crm")
    apply([{"op": "create_table", "name": "thing", "columns": [col("code"), col("name")]}])   # master has it too


def stored(owner, text):
    return storage.save(owner, "dump.sql", text.encode("utf-8"))


def test_the_schema_in_the_file_is_used_when_none_is_chosen(owner, crm):
    r = validate(stored(owner, DUMP), None)
    assert r["ok"], r["errors"]
    assert (r["schema"], r["schema_source"], r["detected_schema"], r["validated_schema"]) == ("crm", "detected", "crm", "crm")


def test_a_chosen_schema_wins_and_the_hint_stays(owner, crm):
    r = validate(stored(owner, DUMP), None, "master")
    assert r["schema"] == "master" and r["schema_source"] == "chosen" and r["detected_schema"] == "crm"


def test_a_schema_that_is_not_a_database_here_falls_back_to_master(owner, crm):
    r = validate(stored(owner, UNKNOWN), None)
    assert (r["schema"], r["schema_source"], r["detected_schema"]) == ("master", "default", "nosuch")


def test_a_file_with_no_schema_uses_master(owner, crm):
    r = validate(stored(owner, PLAIN), None)
    assert (r["schema"], r["schema_source"], r["detected_schema"]) == ("master", "default", None)


def test_an_unknown_chosen_schema_is_still_refused(owner, crm):
    from schema import introspect
    with pytest.raises(introspect.UnknownSchema):
        validate(stored(owner, DUMP), None, "nope")


def test_the_web_api_detects_when_no_schema_is_sent(owner, crm):
    s = stored(owner, DUMP)
    c = Client()
    c.force_login(owner)
    r = c.post(f"/api/scripts/{s.pk}/validate", json.dumps({}), content_type="application/json")
    assert r.status_code == 200 and r.json()["schema"] == "crm" and r.json()["ok"]
    r2 = c.post(f"/api/scripts/{s.pk}/validate", json.dumps({"schema": "master"}), content_type="application/json")
    assert r2.json()["schema"] == "master"


def test_the_mcp_tool_detects_too(owner, crm):
    s = stored(owner, DUMP)
    assert tools.validate_script(owner, s.pk)["report"]["schema"] == "crm"
