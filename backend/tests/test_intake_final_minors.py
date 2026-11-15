"""Final review fixes: submit errors and gate rejections, the MCP get_script and the recipe signal."""
import copy
import json

import pytest

from approvals import service as approvals
from approvals.models import Change
from intake import run as runs
from intake.models import ImportMessage, ImportRecipe, ImportRun
from scripts import storage
from scripts.models import Script

from .test_intake_match import shop  # noqa: F401  (shop is a fixture)
from .test_intake_run import (  # noqa: F401
    FILTERS,
    GOOD,
    MAPPING,
    approver,
    begin,
    editor,
    inline,
    model,
    never_starts,
    person,
    texts,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


# ---------------------------------------------------------------- (d) submit

def test_submit_error_returns_the_run_to_ready(shop, editor, model, inline, monkeypatch):
    from scripts import service as script_service
    r = begin(editor)

    def boom(*a, **k):
        raise approvals.ChangeError("invalid", "schema went away")
    monkeypatch.setattr(script_service, "submit", boom)
    with pytest.raises(runs.RunError) as e:
        runs.submit(editor, r.pk, "")
    assert e.value.code == "invalid" and "schema went away" in e.value.message
    r.refresh_from_db()
    assert r.state == ImportRun.State.READY and r.change_id is None

    def crash(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(script_service, "submit", crash)
    with pytest.raises(runs.RunError) as e:
        runs.submit(editor, r.pk, "")
    assert e.value.code == "bad_state" and "disk on fire" in e.value.message
    r.refresh_from_db()
    assert r.state == ImportRun.State.READY


def test_submit_and_the_run_are_one_transaction(shop, editor, model, inline, monkeypatch):
    r = begin(editor)
    real = runs._set

    def die(run, **fields):
        if fields.get("state") == ImportRun.State.SUBMITTED:
            raise RuntimeError("worker died")
        return real(run, **fields)
    monkeypatch.setattr(runs, "_set", die)
    with pytest.raises(runs.RunError):
        runs.submit(editor, r.pk, "")
    assert not Change.objects.exists()           # no Change without the run knowing it
    assert Script.objects.get(pk=r.report["scriptId"]).status != Script.Status.SUBMITTED
    monkeypatch.setattr(runs, "_set", real)
    change = runs.submit(editor, r.pk, "")
    assert Change.objects.count() == 1 and ImportRun.objects.get(pk=r.pk).change_id == change.pk


def test_a_change_rejected_by_the_gate_is_not_submitted(shop, editor, model, inline, monkeypatch):
    r = begin(editor)
    real = approvals.propose

    def rejected(*a, **k):
        change = real(*a, **k)
        change.status = Change.Status.REJECTED
        change.check_result = {"errors": [], "violations": [{"rule": "no_partners", "severity": "block",
                                                            "message": "partners are frozen"}]}
        change.save()
        return change
    monkeypatch.setattr(approvals, "propose", rejected)
    with pytest.raises(runs.RunError) as e:
        runs.submit(editor, r.pk, "")
    assert "partners are frozen" in e.value.message
    r.refresh_from_db()
    assert r.state == ImportRun.State.FAILED and r.change_id is None
    assert any("partners are frozen" in p for p in r.report["problems"])
    assert not any("waits for an approver" in t for t in texts(r))


# ---------------------------------------------------------------- (f) MCP get_script

def test_mcp_get_script_hides_import_scripts(shop, editor, model, inline):
    from mcp_server import tools
    r = begin(editor)
    with pytest.raises(tools.ToolFailure):
        tools.get_script(editor, r.report["scriptId"])
    plain = storage.save(editor, "plain.sql", b"COPY t (a) FROM stdin;\n1\n\\.\n")
    assert tools.get_script(editor, plain.pk)["id"] == plain.pk


# ---------------------------------------------------------------- (e) the recipe signal

def test_an_older_change_never_overwrites_a_newer_recipe(shop, editor, approver, model, inline):
    a = begin(editor)
    change_a = runs.submit(editor, a.pk, "")
    approvals.approve(approver, change_a.pk)
    newer = copy.deepcopy(GOOD)
    newer["filters"] = {}
    model.replies = [json.dumps(newer)]
    b = begin(editor, text="Vendor Name,Tax ID,Province,Status\nGamma,0105500000444,Phuket,Active\n")
    assert b.state == ImportRun.State.READY, texts(b)
    change_b = runs.submit(editor, b.pk, "")
    approvals.approve(approver, change_b.pk)
    recipe = ImportRecipe.objects.get()
    assert recipe.approved_change_id == change_b.pk and recipe.filters == {}

    Change.objects.get(pk=change_a.pk).save()     # the older applied change is saved again (a note, a revert)
    recipe.refresh_from_db()
    assert recipe.approved_change_id == change_b.pk and recipe.filters == {}


def test_a_failing_recipe_signal_never_blocks_the_approval(shop, editor, approver, model, inline, monkeypatch):
    r = begin(editor)
    change = runs.submit(editor, r.pk, "")

    def boom(*a, **k):
        raise RuntimeError("recipe store down")
    from intake import match as matcher
    monkeypatch.setattr(matcher, "fingerprint", boom)
    done = approvals.approve(approver, change.pk)
    assert done.status == Change.Status.APPLIED
    assert Change.objects.get(pk=change.pk).status == Change.Status.APPLIED
    assert not ImportRecipe.objects.exists()
    assert ImportMessage.objects.filter(run=r).exists()
