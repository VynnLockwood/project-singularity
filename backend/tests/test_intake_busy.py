"""Final review fix: a busy refusal names the open run (its id and database), so the page can lead back to it."""
import pytest

from intake import run as runs

from .test_intake_api import client, post, put_import_file
from .test_intake_match import shop  # noqa: F401  (shop is a fixture)
from .test_intake_run import FILTERS, MAPPING, begin, editor, inline, model, never_starts, person  # noqa: F401

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


# ---------------------------------------------------------------- 5. busy names the open run

def test_busy_reply_names_the_open_run(shop, never_starts):
    u = person("editor@x.io")
    c = client(u)
    body = {"schema": "master", "prompt": ""}
    first = post(c, "/api/imports", {**body, "uploadId": put_import_file(c)})
    assert first.status_code == 201
    again = post(c, "/api/imports", {**body, "uploadId": put_import_file(c)})
    assert again.status_code == 409
    data = again.json()
    assert data["code"] == "busy" and data["runId"] == first.json()["id"] and data["schema"] == "master"


def test_recheck_busy_names_the_other_run(shop, editor, model, inline, monkeypatch):
    from .test_intake_run import bad_column
    model.replies = [bad_column()]
    a = begin(editor)
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *x, **k: None)
    b = begin(editor)
    with pytest.raises(runs.RunError) as e:
        runs.recheck(editor, a.pk, MAPPING, FILTERS, {})
    assert e.value.code == "busy" and e.value.extra == {"runId": b.pk, "schema": "master"}
