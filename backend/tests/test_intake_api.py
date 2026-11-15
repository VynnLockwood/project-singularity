"""Task 8: the import endpoints (/api/imports...), over runs started with a fake provider and an in-process child."""
import json

import pytest
from django.test import Client

from accounts.models import Role
from approvals.models import Change
from intake.models import ImportRun
from scripts.models import Script

from .test_intake_run import (FILTERS, MAPPING, VENDORS_CSV, Model, good, inline, model, never_starts,  # noqa: F401
                              person, shop)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

SECRET = "sk-or-secret-1234567890"


def client(user):
    c = Client()
    c.force_login(user)
    return c


def post(c, url, body=None):
    return c.post(url, data=json.dumps(body if body is not None else {}), content_type="application/json")


def put_import_file(c, text=VENDORS_CSV, name="vendors.csv"):
    """The way the browser does it: begin with kind import, then the chunks. No complete call."""
    data = text.encode()
    r = post(c, "/api/scripts/uploads", {"name": name, "size": len(data), "kind": "import"})
    assert r.status_code == 201, r.content
    uid = r.json()["upload_id"]
    assert c.put(f"/api/scripts/uploads/{uid}/0", data=data, content_type="application/octet-stream").status_code == 200
    return uid


def start(c, **over):
    body = {"uploadId": put_import_file(c), "schema": "master", "prompt": "Load the vendors; only the active ones."}
    body.update(over)
    return post(c, "/api/imports", body)


@pytest.fixture
def ed():
    u = person("editor@x.io")
    return client(u), u


# ---------------------------------------------------------------- the full flow

def test_full_flow_to_a_pending_change(shop, ed, model, inline):
    c, _ = ed
    seen = []
    r = start(c, apiKey=SECRET)
    seen.append(r.content)
    assert r.status_code == 201, r.content
    run = r.json()
    assert run["state"] == "ready" and run["schema"] == "master" and run["fileName"] == "vendors.csv"
    assert run["round"] == 1 and run["changeId"] is None
    assert set(run) >= {"id", "schema", "state", "prompt", "fileName", "round", "profile", "matches", "plan",
                        "report", "messages", "expiresAt"}
    assert [m["role"] for m in run["messages"]] == ["profiler", "matcher", "designer", "critic"]
    assert set(run["messages"][0]) == {"role", "text", "details", "at"}
    assert run["report"]["ok"] is True and run["plan"]["mapping"] == MAPPING

    g = c.get(f"/api/imports/{run['id']}")
    seen.append(g.content)
    assert g.status_code == 200 and g.json()["messages"] == run["messages"]

    rc = post(c, f"/api/imports/{run['id']}/recheck", {"mapping": MAPPING, "filters": FILTERS, "skip": {"vendors": ["3"]}})
    seen.append(rc.content)
    assert rc.status_code == 200, rc.content
    assert rc.json()["state"] == "ready" and rc.json()["plan"]["picked"] == {"vendors": [3]}

    s = post(c, f"/api/imports/{run['id']}/submit", {"note": "vendors from November"})
    seen.append(s.content)
    assert s.status_code == 201, s.content
    change = Change.objects.get(pk=s.json()["changeId"])
    assert change.status == Change.Status.PENDING and change.schema == "master"
    g = c.get(f"/api/imports/{run['id']}")
    seen.append(g.content)
    assert g.json()["state"] == "submitted" and g.json()["changeId"] == change.pk
    assert g.json()["messages"][-1]["details"] == {"changeId": change.pk}

    for body in seen:                       # the key is in no response, nor in the stored run
        assert SECRET.encode() not in body
    for rr in ImportRun.objects.all():
        assert SECRET not in json.dumps(c.get(f"/api/imports/{rr.pk}").json())


def test_questions_then_answer_with_the_key_again(shop, ed, model, inline):
    c, _ = ed
    model.replies = [json.dumps({"mapping": {}, "filters": {}, "reasons": {}, "questions": ["Load inactive vendors?"]}),
                     good()]
    run = start(c, apiKey=SECRET).json()
    assert run["state"] == "waiting_for_answer" and run["plan"]["questions"] == ["Load inactive vendors?"]
    r = post(c, f"/api/imports/{run['id']}/answer", {"text": "Only the active ones.", "apiKey": SECRET})
    assert r.status_code == 200, r.content
    assert r.json()["state"] == "ready" and SECRET not in r.content.decode()
    assert model.calls[-1]["key"] == SECRET
    assert "person" in [m["role"] for m in r.json()["messages"]]


def test_delete_cancels_a_ready_run(shop, ed, model, inline):
    c, _ = ed
    run = start(c).json()
    assert c.delete(f"/api/imports/{run['id']}").status_code == 204
    assert c.get(f"/api/imports/{run['id']}").status_code == 404
    assert start(c).status_code == 201      # no longer busy


# ---------------------------------------------------------------- who may

def test_a_viewer_gets_403_everywhere(shop, ed, model, inline):
    c, _ = ed
    run = start(c).json()
    v = client(person("viewer@x.io", Role.VIEWER))
    rid = run["id"]
    assert post(v, "/api/imports", {"uploadId": 1}).status_code == 403
    assert v.get(f"/api/imports/{rid}").status_code == 403
    assert v.delete(f"/api/imports/{rid}").status_code == 403
    for tail in ("answer", "recheck", "submit"):
        assert post(v, f"/api/imports/{rid}/{tail}", {"text": "x", "mapping": {}}).status_code == 403


def test_anonymous_gets_401(shop):
    assert Client().get("/api/imports/1").status_code == 401
    assert post(Client(), "/api/imports", {}).status_code == 401


def test_another_editor_cannot_see_or_touch_a_run(shop, ed, model, inline):
    c, _ = ed
    run = start(c).json()
    rid = run["id"]
    other = client(person("other@x.io"))
    assert other.get(f"/api/imports/{rid}").status_code == 404
    assert other.delete(f"/api/imports/{rid}").status_code == 404
    assert post(other, f"/api/imports/{rid}/answer", {"text": "x"}).status_code == 404
    assert post(other, f"/api/imports/{rid}/recheck", {"mapping": MAPPING, "filters": FILTERS}).status_code == 404
    assert post(other, f"/api/imports/{rid}/submit", {}).status_code == 404
    assert other.get(f"/api/imports/{rid}").content.replace(str(rid).encode(), b"N") == \
        other.get("/api/imports/999999").content.replace(b"999999", b"N")     # no tell between "yours" and "absent"
    assert ImportRun.objects.get(pk=rid).state == "ready"
    assert other.get(f"/api/imports/{rid}").status_code == 404


def test_an_approver_may_read_and_submit_anothers_run(shop, ed, model, inline):
    c, _ = ed
    rid = start(c).json()["id"]
    a = client(person("approver@x.io", Role.APPROVER))
    g = a.get(f"/api/imports/{rid}")
    assert g.status_code == 200 and g.json()["id"] == rid and SECRET not in g.content.decode()
    assert post(a, f"/api/imports/{rid}/submit", {}).status_code == 201


# ---------------------------------------------------------------- limits and shapes

def test_input_limits_are_400(shop, ed, model, never_starts):
    c, _ = ed
    uid = put_import_file(c)                # a refused request leaves the upload open, so one file serves them all

    def go(**over):
        return post(c, "/api/imports", {"uploadId": uid, "schema": "master", "prompt": "", **over})
    assert go(prompt="p" * 2001).status_code == 400
    assert go(apiKey="k" * 501).status_code == 400
    assert go(prompt=5).status_code == 400
    assert go(apiKey=5).status_code == 400
    assert go(uploadId="x").status_code == 400
    assert go(uploadId=True).status_code == 400
    assert go(schema="nope").status_code == 400
    assert go(uploadId=999999).status_code == 404
    assert not ImportRun.objects.exists()
    assert go(prompt="p" * 2000, apiKey="k" * 500).status_code == 201


def test_answer_limits_are_400(shop, ed, model, inline):
    c, _ = ed
    model.replies = [json.dumps({"mapping": {}, "filters": {}, "reasons": {}, "questions": ["Q?"]}), good()]
    rid = start(c).json()["id"]
    url = f"/api/imports/{rid}/answer"
    assert post(c, url, {"text": "a" * 1001}).status_code == 400
    assert post(c, url, {"text": "ok", "apiKey": "k" * 501}).status_code == 400
    assert post(c, url, {"text": ""}).status_code == 400
    assert post(c, url, {}).status_code == 400
    assert ImportRun.objects.get(pk=rid).state == "waiting_for_answer"
    assert post(c, url, {"text": "a" * 1000}).status_code == 200


def test_recheck_bad_shapes_are_400_not_500(shop, ed, model, inline):
    c, _ = ed
    rid = start(c).json()["id"]
    url = f"/api/imports/{rid}/recheck"
    good_body = {"mapping": MAPPING, "filters": FILTERS, "skip": {}}
    for bad in ({"mapping": [], "filters": {}}, {"mapping": "x"}, {"filters": {}}, {**good_body, "filters": []},
                {**good_body, "skip": []}, {**good_body, "skip": {"vendors": "3"}},
                {**good_body, "skip": {"vendors": ["x"]}}, {**good_body, "skip": {"vendors": [0]}},
                {**good_body, "skip": {"vendors": [True]}}, {"mapping": {"tables": []}, "filters": {}},
                {"mapping": {"tables": {"vendors": 5}}, "filters": {}}):
        r = post(c, url, bad)
        assert r.status_code == 400, (bad, r.status_code, r.content)
    assert c.post(url, data="[1]", content_type="application/json").status_code == 400
    assert c.post(url, data="{bad", content_type="application/json").status_code == 400
    assert ImportRun.objects.get(pk=rid).state == "ready"
    assert post(c, url, {**good_body, "skip": {"vendors": ["3", 2]}}).status_code == 200


# ---------------------------------------------------------------- states

def test_wrong_state_is_409(shop, ed, model, inline):
    c, _ = ed
    rid = start(c).json()["id"]                                     # ready
    assert post(c, f"/api/imports/{rid}/answer", {"text": "hi"}).status_code == 409
    assert post(c, f"/api/imports/{rid}/submit", {}).status_code == 201
    assert post(c, f"/api/imports/{rid}/submit", {}).status_code == 409
    assert post(c, f"/api/imports/{rid}/recheck", {"mapping": MAPPING, "filters": FILTERS}).status_code == 409
    assert c.delete(f"/api/imports/{rid}").status_code == 409       # a submitted run is kept


def test_running_run_cannot_be_deleted_submitted_or_answered(shop, ed, model, never_starts):
    c, _ = ed
    r = start(c)
    assert r.status_code == 201 and r.json()["state"] == "profiling"
    rid = r.json()["id"]
    assert c.delete(f"/api/imports/{rid}").status_code == 409
    assert post(c, f"/api/imports/{rid}/submit", {}).status_code == 409
    assert post(c, f"/api/imports/{rid}/answer", {"text": "x"}).status_code == 409
    assert post(c, f"/api/imports/{rid}/recheck", {"mapping": MAPPING, "filters": FILTERS}).status_code == 409
    assert ImportRun.objects.filter(pk=rid).exists()


def test_a_second_open_run_is_409(shop, ed, model, never_starts):
    c, _ = ed
    assert start(c).status_code == 201
    r = start(c)
    assert r.status_code == 409 and r.json()["code"] == "busy"


def test_wrong_methods_are_405(shop, ed):
    c, _ = ed
    assert c.get("/api/imports").status_code == 405
    assert c.put("/api/imports/1", data="{}", content_type="application/json").status_code == 405
    assert c.get("/api/imports/1/submit").status_code == 405


# ---------------------------------------------------------------- uploads of kind import

def test_upload_kinds(shop, ed, model, inline):
    c, _ = ed
    assert post(c, "/api/scripts/uploads", {"name": "a.csv", "size": 5, "kind": "bogus"}).status_code == 400
    assert post(c, "/api/scripts/uploads", {"name": "a.csv", "size": 5, "kind": 7}).status_code == 400
    assert post(c, "/api/scripts/uploads", {"name": "a.sql", "size": 5, "kind": "import"}).status_code == 400
    ok = post(c, "/api/scripts/uploads", {"name": "a.sql", "size": 5})            # default stays a script upload
    assert ok.status_code == 201

    uid = put_import_file(c)
    done = post(c, f"/api/scripts/uploads/{uid}/complete")                        # not completable as a script
    assert done.status_code == 400 and not Script.objects.exists()
    r = post(c, "/api/imports", {"uploadId": uid, "schema": "master", "prompt": ""})
    assert r.status_code == 201 and r.json()["state"] == "ready"                  # but start() can use it
    assert c.get("/api/scripts").json() in ([], {"scripts": []}) or \
        all("import-" not in s.get("name", "") for s in c.get("/api/scripts").json().get("scripts", []))


def test_another_persons_upload_cannot_be_started(shop, ed, model, never_starts):
    c, _ = ed
    uid = put_import_file(c)
    other = client(person("other@x.io"))
    assert post(other, "/api/imports", {"uploadId": uid, "schema": "master"}).status_code == 404
