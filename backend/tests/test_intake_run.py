"""Task 7: the Designer (chat model, faked here), the Critic (scripts validation) and the import run."""
import copy
import json
import os
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from accounts.models import Role, User
from approvals import service as approvals
from approvals.models import Change
from chat import providers
from config import siconfig
from intake import design
from intake import match as matcher
from intake import run as runs
from intake.models import ImportMessage, ImportRecipe, ImportRun
from scripts import storage, uploads
from scripts.models import Script

from .test_intake_match import build, shop  # noqa: F401  (shop is a fixture)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

KEY_ENV = "SINGULARITY_IMPORT_KEY"

VENDORS_CSV = (
    "Vendor Name,Tax ID,Province,Status\n"
    '"Acme Co., Ltd.",0105500000099,Bangkok,Active\n'     # row 1: maybe the same as Acme Company Limited
    "Siam Steel Trading,0105500000002,Chiang Mai,Active\n"  # row 2: tax id exists -> skipped
    "Brand New Shop,0105500000123,Phuket,Active\n"          # row 3: new
    "Old Closed Shop,0105500000124,Phuket,Inactive\n"       # row 4: removed by the filter
)
HEADERS = ["Vendor Name", "Tax ID", "Province", "Status"]

MAPPING = {"tables": {"vendors": {
    "target": "partner",
    "columns": {"name": "Vendor Name", "tax_id": "Tax ID"},
    "drop": ["Status"],
    "refs": [{"column": "province_id", "parent": "province", "parent_key": "name",
              "source_column": "Province", "external": True}],
}}}
FILTERS = {"vendors": [{"column": "Status", "op": "equals", "value": "Active"}]}
GOOD = {"mapping": MAPPING, "filters": FILTERS,
        "reasons": {"vendors": "vendors are partners", "vendors.Tax ID": "same 13-digit tax id"},
        "questions": []}


def good(**over):
    out = copy.deepcopy(GOOD)
    out.update(over)
    return json.dumps(out)


def bad_column():
    m = copy.deepcopy(MAPPING)
    m["tables"]["vendors"]["columns"] = {"name": "Vendor Name", "nope_column": "Tax ID"}
    return good(mapping=m)


class Model:
    """Stands in for the provider; the last reply repeats."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, provider, messages, tools, model, key=""):
        self.calls.append({"messages": copy.deepcopy(messages), "model": model, "key": key, "tools": tools})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"content": text, "tool_calls": []}

    def prompt(self, n):
        """Everything sent in call n, as one string."""
        return "\n".join(m["content"] for m in self.calls[n]["messages"])


@pytest.fixture
def model(monkeypatch):
    m = Model(good())
    monkeypatch.setattr(providers, "complete", m)
    real = siconfig.get_config

    def cfg():
        c = real()
        c["chat"]["defaultModel"] = "test-model"
        return c

    monkeypatch.setattr(design, "get_config", cfg)
    return m


@pytest.fixture
def inline(monkeypatch):
    """The child process runs in this process: argv and env are recorded, the key arrives through the env."""
    calls = []

    def fake(argv, env=None, **kw):
        calls.append({"argv": list(argv), "env": dict(env or {})})
        old = os.environ.get(KEY_ENV)
        if env and KEY_ENV in env:
            os.environ[KEY_ENV] = env[KEY_ENV]
        else:
            os.environ.pop(KEY_ENV, None)
        try:
            call_command("run_import", argv[-1])
        finally:
            if old is None:
                os.environ.pop(KEY_ENV, None)
            else:
                os.environ[KEY_ENV] = old
    monkeypatch.setattr(runs.subprocess, "Popen", fake)
    return calls


@pytest.fixture
def never_starts(monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: None)


def person(email, role=Role.EDITOR):
    return User.objects.create(username=email, email=email, role=role)


@pytest.fixture
def editor():
    return person("editor@x.io")


@pytest.fixture
def approver():
    return person("approver@x.io", Role.APPROVER)


def upload(user, text=VENDORS_CSV, name="vendors.csv"):
    data = text.encode("utf-8")
    b = uploads.begin(user, name, len(data), kind="import")
    uploads.put_chunk(user, b["upload_id"], 0, data)
    return b["upload_id"]


def begin(user, prompt="Load the vendors; only the active ones.", api_key="", text=VENDORS_CSV, schema="master"):
    r = runs.start(user, upload(user, text), schema, prompt, api_key=api_key)
    return ImportRun.objects.get(pk=r.pk)


def roles(r):
    return list(r.messages.values_list("role", flat=True))


def texts(r, role=None):
    qs = r.messages.all() if role is None else r.messages.filter(role=role)
    return [m.text for m in qs]


# ---------------------------------------------------------------- the happy path

def test_happy_path_reaches_ready(shop, editor, model, inline):
    r = begin(editor)
    assert r.state == ImportRun.State.READY, texts(r)
    assert roles(r) == ["profiler", "matcher", "designer", "critic"]
    assert r.round == 1 and len(model.calls) == 1
    assert model.calls[0]["model"] == "test-model" and model.calls[0]["tools"] == []

    profiler, matcher_text, designer, critic = texts(r)
    assert "vendors" in profiler and "4 rows" in profiler and "4 columns" in profiler
    assert "partner" in matcher_text and "1 already exist" in matcher_text and "1 may" in matcher_text
    assert "partner" in designer and "Tax ID" in designer
    assert critic.startswith("Passed")

    t = r.report["tables"]["vendors"]
    assert r.report["ok"] is True
    assert (t["target"], t["rows"], t["new"], t["exists"], t["filtered"], t["skipped"]) == \
        ("partner", 4, 2, 1, 1, 0)
    assert [m["row"] for m in t["maybe"]] == [1]     # loaded by default, surfaced
    assert r.plan["skip"] == {"vendors": [2]}       # the existing row, by its original index
    assert r.plan["mapping"] == MAPPING and r.plan["filters"] == FILTERS
    assert r.plan["reasons"]["vendors"] == "vendors are partners"
    assert r.sources[0]["name"] == "vendors" and r.sources[0]["headers"] == HEADERS
    assert "rows" not in r.sources[0]                # no row data in the database
    # the command was spawned the scripts.jobs way
    assert inline[0]["argv"][-2:] == ["run_import", str(r.pk)]


# ---------------------------------------------------------------- Review Focus 3

def test_bad_designer_output_never_ready(shop, editor, model, inline):
    # non-JSON twice -> one retry with the error, then failed with the reason
    model.replies = ["Sure! Here is the plan: mapping partner", "still not json"]
    r = begin(editor)
    assert r.state == ImportRun.State.FAILED
    assert len(model.calls) == 2
    assert "not a valid plan" in model.calls[1]["messages"][-1]["content"].lower() or \
        "json" in model.calls[1]["messages"][-1]["content"].lower()
    assert any("the model did not return a valid plan" in t for t in texts(r))
    assert "critic" not in roles(r)

    # a mapping to an unknown column -> the critic's feedback is in the round-2 prompt; 3 bad rounds -> failed
    model.calls.clear()
    model.replies = [bad_column()]
    r = begin(editor)
    assert r.state == ImportRun.State.FAILED, texts(r)
    assert len(model.calls) == 3 and r.round == 3
    critic = texts(r, "critic")
    assert len(critic) == 3 and all(not c.startswith("Passed") for c in critic)
    assert "nope_column" in critic[0]
    assert "nope_column" not in model.prompt(0)
    assert "nope_column" in model.prompt(1) and "Problems" in model.prompt(1)
    assert r.report["ok"] is False and r.report["problems"]
    assert all(len(p) <= 300 for p in r.report["problems"]) and len(r.report["problems"]) <= 20
    assert any("round" in t.lower() for t in texts(r, "system"))


def test_malformed_plan_shapes_never_crash(shop, editor, model, inline):
    # wrong JSON types are a parse failure (retried once), not a crash
    model.replies = [json.dumps({"mapping": "partner", "filters": [], "reasons": {}, "questions": []}),
                     json.dumps({"mapping": {"tables": {"vendors": "partner"}}, "filters": {}, "reasons": {},
                                 "questions": []})]
    r = begin(editor)
    assert r.state == ImportRun.State.FAILED and len(model.calls) == 2
    assert any("the model did not return a valid plan" in t for t in texts(r))

    # a filter on a column the sheet does not have: the Critic reports it, the run goes on to the next round
    model.calls.clear()
    model.replies = [good(filters={"vendors": [{"column": "Nope", "op": "equals", "value": "x"}]}), good()]
    r = begin(editor)
    assert r.state == ImportRun.State.READY, texts(r)
    assert "Nope" in texts(r, "critic")[0] and "Nope" in model.prompt(1)


def test_chat_error_fails_the_run(shop, editor, model, inline, monkeypatch):
    from chat.errors import ChatError

    def boom(*a, **k):
        raise ChatError("provider_error", "upstream is down")
    monkeypatch.setattr(providers, "complete", boom)
    r = begin(editor)
    assert r.state == ImportRun.State.FAILED
    assert any("upstream is down" in t for t in texts(r))


def test_no_model_configured(shop, editor, inline, monkeypatch):
    monkeypatch.setattr(providers, "complete", Model(good()))
    real = siconfig.get_config

    def cfg():
        c = real()
        c["chat"]["defaultModel"] = ""
        c["import"]["model"] = ""
        return c

    monkeypatch.setattr(design, "get_config", cfg)
    r = begin(editor)
    assert r.state == ImportRun.State.FAILED
    assert any("no chat model is configured" in t for t in texts(r))


# ---------------------------------------------------------------- questions

def test_questions_pause_and_resume(shop, editor, model, inline):
    model.replies = [json.dumps({"mapping": {}, "filters": {}, "reasons": {},
                                 "questions": ["Is 'Tax ID' the vendor's tax id?", "Load inactive vendors?",
                                               "Third?", "A fourth is dropped"]}),
                     good()]
    r = begin(editor)
    assert r.state == ImportRun.State.WAITING_FOR_ANSWER
    assert roles(r) == ["profiler", "matcher", "designer"]
    assert "Is 'Tax ID' the vendor's tax id?" in texts(r, "designer")[0]
    assert len(r.plan["questions"]) == 3 and "A fourth" not in texts(r, "designer")[0]

    runs.answer(editor, r.pk, "Yes, the 13-digit tax id. Only active ones.")
    r.refresh_from_db()
    assert r.state == ImportRun.State.READY, texts(r)
    assert roles(r) == ["profiler", "matcher", "designer", "person", "designer", "critic"]
    assert r.answers[-1]["text"] == "Yes, the 13-digit tax id. Only active ones."
    assert "Yes, the 13-digit tax id" in model.prompt(1) and "Is 'Tax ID' the vendor's tax id?" in model.prompt(1)

    with pytest.raises(runs.RunError) as e:
        runs.answer(editor, r.pk, "again")
    assert e.value.code == "bad_state"


# ---------------------------------------------------------------- Review Focus 4

def test_recipe_drift_is_repaired(shop, editor, model, inline):
    build("ALTER TABLE master.partner ADD COLUMN region text")
    old = copy.deepcopy(MAPPING)
    old["tables"]["vendors"]["columns"]["region"] = "Status"
    old["tables"]["vendors"]["drop"] = []
    applied = Change.objects.create(requester=editor, source="script", kind="data", status=Change.Status.APPLIED)
    recipe = ImportRecipe.objects.create(schema="master", fingerprint=matcher.fingerprint(HEADERS), headers=HEADERS,
                                         mapping=old, filters={}, approved_change=applied)
    build("ALTER TABLE master.partner DROP COLUMN region")

    r = begin(editor)
    assert r.state == ImportRun.State.READY, texts(r)
    assert r.recipe_id == recipe.pk
    assert roles(r) == ["profiler", "matcher", "critic", "designer", "critic"]
    drift = texts(r, "critic")[0]
    assert "recipe" in drift.lower() and "drift" in drift.lower() and "region" in drift
    first = model.prompt(0)
    assert "region" in first.split("Problems")[-1]       # the drift is the round-1 feedback
    assert '"region": "Status"' in first or "region" in first.split("recipe")[-1]
    assert r.plan["mapping"] == MAPPING                    # repaired, not the recipe applied blindly


# ---------------------------------------------------------------- the person's edits

def test_recheck_after_edit_skips_designer(shop, editor, model, inline):
    r = begin(editor)
    assert r.state == ImportRun.State.READY
    n = len(model.calls)
    runs.recheck(editor, r.pk, MAPPING, {}, {"vendors": ["3"]})     # strings from the API become ints
    r.refresh_from_db()
    assert r.state == ImportRun.State.READY, texts(r)
    assert len(model.calls) == n
    assert roles(r)[-1] == "critic" and "designer" not in roles(r)[4:]
    assert r.plan["skip"] == {"vendors": [2, 3]} and r.plan["filters"] == {}
    t = r.report["tables"]["vendors"]
    assert (t["new"], t["exists"], t["filtered"], t["skipped"]) == (2, 1, 0, 1)

    bad = copy.deepcopy(MAPPING)
    bad["tables"]["vendors"]["columns"]["nope_column"] = "Status"
    bad["tables"]["vendors"]["drop"] = []
    runs.recheck(editor, r.pk, bad, {}, {})
    r.refresh_from_db()
    assert r.state == ImportRun.State.FAILED and "nope_column" in texts(r, "critic")[-1]
    assert len(model.calls) == n

    with pytest.raises(runs.RunError) as e:
        runs.recheck(editor, r.pk, MAPPING, {}, {"vendors": ["x"]})
    assert e.value.code == "invalid"


# ---------------------------------------------------------------- submit and the recipe

def stored_script(change):
    return storage.read(Script.objects.get(change=change)).decode()


def test_submit_creates_one_pending_change(shop, editor, model, inline):
    r = begin(editor)
    runs.recheck(editor, r.pk, MAPPING, FILTERS, {"vendors": [3]})
    before = Change.objects.count()
    change = runs.submit(editor, r.pk, "vendors from November")
    r.refresh_from_db()
    assert Change.objects.count() == before + 1
    assert change.status == Change.Status.PENDING and change.schema == "master"
    assert r.state == ImportRun.State.SUBMITTED and r.change_id == change.pk
    text = stored_script(change)
    assert "Acme Co., Ltd." in text
    assert "Siam Steel Trading" not in text      # already exists
    assert "Old Closed Shop" not in text         # removed by the filter
    assert "Brand New Shop" not in text          # skipped by the person
    with pytest.raises(runs.RunError) as e:
        runs.submit(editor, r.pk, "")
    assert e.value.code == "bad_state"


def test_submit_refuses_when_no_longer_valid(shop, editor, model, inline):
    r = begin(editor)
    build("ALTER TABLE master.partner DROP COLUMN tax_id")
    with pytest.raises(runs.RunError) as e:
        runs.submit(editor, r.pk, "")
    assert e.value.code == "invalid"
    r.refresh_from_db()
    assert r.state == ImportRun.State.FAILED and r.change_id is None


def test_recipe_saved_when_applied(shop, editor, approver, model, inline):
    r = begin(editor)
    change = runs.submit(editor, r.pk, "")
    assert not ImportRecipe.objects.exists()
    done = approvals.approve(approver, change.pk)
    assert done.status == Change.Status.APPLIED
    recipe = ImportRecipe.objects.get(schema="master", fingerprint=matcher.fingerprint(HEADERS))
    assert recipe.mapping == MAPPING and recipe.filters == FILTERS
    assert recipe.approved_change_id == change.pk and recipe.headers == HEADERS
    from schema import conn
    with conn.rw_connection(autocommit=True) as c:
        names = {x["name"] for x in c.execute("SELECT name FROM master.partner")}
    assert {"Acme Co., Ltd.", "Brand New Shop"} <= names and "Old Closed Shop" not in names

    # the next file of this shape starts from the recipe
    r2 = begin(editor)
    assert r2.recipe_id == recipe.pk


# ---------------------------------------------------------------- one open run, the key, the profiles

def test_one_open_run_per_user_and_schema(shop, editor, never_starts):
    r = begin(editor)
    assert r.state == ImportRun.State.PROFILING
    with pytest.raises(runs.RunError) as e:
        begin(editor)
    assert e.value.code == "busy"
    assert begin(person("other@x.io")).state == ImportRun.State.PROFILING

    # a stale heartbeat reads as failed, and then a new run may start
    ImportRun.objects.filter(pk=r.pk).update(heartbeat=timezone.now() - timedelta(seconds=601))
    assert runs.get(editor, r.pk).state == ImportRun.State.FAILED
    assert begin(editor).state == ImportRun.State.PROFILING

    viewer = person("viewer@x.io", Role.VIEWER)
    with pytest.raises(runs.RunError) as e:
        runs.get(viewer, r.pk)
    assert e.value.code == "forbidden"
    with pytest.raises(runs.RunError) as e:
        runs.get(person("third@x.io"), r.pk)
    assert e.value.code == "forbidden"


def test_api_key_not_stored(shop, editor, model, inline):
    secret = "sk-or-secret-1234567890"
    r = begin(editor, api_key=secret)
    assert r.state == ImportRun.State.READY
    assert all(c["key"] == secret for c in model.calls)
    assert inline[0]["env"][KEY_ENV] == secret and secret not in " ".join(inline[0]["argv"])
    runs.submit(editor, r.pk, "")
    r.refresh_from_db()
    fields = [f.name for f in ImportRun._meta.get_fields() if getattr(f, "get_internal_type", None)
              and f.get_internal_type() in ("JSONField", "TextField", "CharField")]
    for name in fields:
        assert secret not in json.dumps(getattr(r, name), default=str), name
    for m in ImportMessage.objects.all():
        assert secret not in m.text and secret not in json.dumps(m.details)
    for s in Script.objects.all():
        assert secret not in json.dumps(s.report) and secret not in json.dumps(s.mapping)
    for c in Change.objects.all():
        assert secret not in c.prompt and secret not in json.dumps(c.operations, default=str)
    assert os.environ.get(KEY_ENV) is None


def test_profiles_only_reach_the_model(shop, editor, model, inline):
    lines = ["Vendor Name,Tax ID,Province,Status"]
    for i in range(1, 13):
        lines.append(f"Shop {i:02d} Unique,01055000009{i:02d},Phuket,Active")
    r = begin(editor, text="\n".join(lines) + "\n")
    assert len(model.calls) >= 1
    for n in range(len(model.calls)):
        sent = model.prompt(n)
        names = [f"Shop {i:02d} Unique" for i in range(1, 13) if f"Shop {i:02d} Unique" in sent]
        ids = [f"01055000009{i:02d}" for i in range(1, 13) if f"01055000009{i:02d}" in sent]
        assert len(names) <= 5 and len(ids) <= 5, (names, ids)
    assert r.state in (ImportRun.State.READY, ImportRun.State.FAILED)


# ---------------------------------------------------------------- expiry

def test_expired_runs_are_purged_with_their_files(shop, editor, never_starts):
    r = begin(editor)
    assert os.path.exists(r.file_path)
    ImportRun.objects.filter(pk=r.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert runs.purge_expired() == 1
    assert not ImportRun.objects.filter(pk=r.pk).exists() and not os.path.exists(r.file_path)
    assert runs.purge_expired() == 0
