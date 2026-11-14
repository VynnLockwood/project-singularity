import pytest
from django.core.cache import cache
from django.test import Client

from accounts.models import Role, User
from chat import providers
from iquery import suggest as suggest_mod
from iquery.models import QueryExample
from mcp_server import tools

from .test_iquery_context import build, shop  # noqa: F401  (shop is a fixture)
from .test_iquery_suggest import OK, Model, patched_config  # noqa: F401

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

BASE = "/api/intelligent-query"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()


def login(role, name="v"):
    u = User.objects.create(username=f"{name}@x.com", email=f"{name}@x.com", role=role)
    c = Client()
    c.force_login(u)
    return c, u


@pytest.fixture
def viewer():
    return login(Role.VIEWER)


@pytest.fixture
def approver():
    return login(Role.APPROVER, "a")


@pytest.fixture
def model(monkeypatch):
    m = Model(OK)
    monkeypatch.setattr(providers, "complete", m)
    patched_config(monkeypatch, iq={}, chat={"defaultModel": "m", "provider": "openai"})
    return m


def post(c, path, **body):
    return c.post(f"{BASE}/{path}", body, content_type="application/json")


def test_viewer_can_suggest_and_request_id_is_echoed(shop, viewer, model):
    c, _ = viewer
    r = post(c, "suggest", schema="master", comments=["contracts"], requestId="r1")
    assert r.status_code == 200, r.content
    assert r.json()["sql"] == OK and r.json()["requestId"] == "r1"


def test_viewer_can_recall_nothing(shop, viewer, model):
    r = post(viewer[0], "recall", schema="master", comments=["contracts"], requestId="r2")
    assert r.status_code == 200 and r.json()["sql"] is None and r.json()["requestId"] == "r2"


def test_comment_limits(shop, viewer, model):
    c, _ = viewer
    many = [f"c{i}" for i in range(15)]
    assert post(c, "suggest", schema="master", comments=many).status_code == 200
    text = model.calls[-1]["messages"][1]["content"]
    assert "c14" in text and "c5" in text and "c4\n" not in text and "c0" not in text
    post(c, "suggest", schema="master", comments=["x" * 900])
    assert "x" * 500 in model.calls[-1]["messages"][1]["content"]
    assert "x" * 501 not in model.calls[-1]["messages"][1]["content"]


def test_sql_context_keeps_the_tail(shop, viewer, model):
    ctx = "A" * 5000 + "TAILMARK"
    post(viewer[0], "suggest", schema="master", comments=["c"], sqlContext=ctx)
    text = model.calls[-1]["messages"][1]["content"]
    assert "TAILMARK" in text and "A" * 4001 not in text


@pytest.mark.parametrize("body", [
    {"schema": "master", "comments": []},
    {"schema": "master", "comments": "x"},
    {"schema": "master", "comments": [1]},
    {"schema": "master", "comments": ["x"], "apiKey": "k" * 501},
    {"schema": "nope", "comments": ["x"]},
])
def test_bad_input_is_400(shop, viewer, model, body):
    assert post(viewer[0], "suggest", **body).status_code == 400


def test_api_key_goes_to_provider_and_is_not_echoed(shop, viewer, model):
    r = post(viewer[0], "suggest", schema="master", comments=["c"], apiKey="sk-secret")
    assert model.calls[-1]["key"] == "sk-secret"
    assert "sk-secret" not in r.content.decode()


def test_status_codes(shop, viewer, monkeypatch):
    c, _ = viewer
    patched_config(monkeypatch, iq={}, chat={"defaultModel": "", "provider": "openai"})
    assert post(c, "suggest", schema="master", comments=["c"]).status_code == 503
    m = Model(OK)
    monkeypatch.setattr(providers, "complete", m)
    patched_config(monkeypatch, iq={"ratePerMinute": 1}, chat={"defaultModel": "m", "provider": "openai"})
    assert post(c, "suggest", schema="master", comments=["a"]).status_code == 200
    assert post(c, "suggest", schema="master", comments=["b"]).status_code == 429


def test_learn_stores_candidate_and_rejects_non_select(shop, viewer):
    c, _ = viewer
    r = post(c, "learn", schema="master", comments=["contracts"], sql=OK, values=[])
    assert r.status_code == 200 and r.json()["status"] == "candidate"
    assert post(c, "learn", schema="master", comments=["c"], sql='DELETE FROM "contract"', values=[]).status_code == 400
    assert post(c, "learn", schema="master", comments=["c"], sql=OK + "; SELECT 1", values=[]).status_code == 400
    assert QueryExample.objects.count() == 1


def test_examples_are_approver_only(shop, viewer, approver):
    post(viewer[0], "learn", schema="master", comments=["contracts"], sql=OK, values=[])
    assert viewer[0].get(f"{BASE}/examples", {"schema": "master"}).status_code == 403
    listed = approver[0].get(f"{BASE}/examples", {"schema": "master"}).json()["examples"]
    assert len(listed) == 1 and listed[0]["sql"] == OK and listed[0]["status"] == "candidate"
    assert viewer[0].delete(f"{BASE}/examples/{listed[0]['id']}").status_code == 403
    assert approver[0].delete(f"{BASE}/examples/{listed[0]['id']}").status_code == 204
    assert QueryExample.objects.count() == 0
    assert approver[0].delete(f"{BASE}/examples/{listed[0]['id']}").status_code == 404


def test_signed_out_is_401():
    assert Client().post(f"{BASE}/suggest", {}, content_type="application/json").status_code == 401


def test_mcp_tool_matches_endpoint_shape(shop, viewer, model):
    _, u = viewer
    out = tools.suggest_query(u, "master", ["contracts"])
    http = post(viewer[0], "suggest", schema="master", comments=["contracts"]).json()
    assert set(out) == set(http) - {"requestId"}
    assert tools.allowed(u, "suggest_query")
    with pytest.raises(tools.ToolFailure):
        tools.suggest_query(u, "master", [])
