import pytest
from django.core.cache import cache

from accounts.models import Role, User
from chat import providers
from chat.errors import ChatError
from config import siconfig
from iquery import memory, suggest
from iquery.models import QueryExample, SuggestCall

from .conftest import fake_vector
from .test_iquery_context import build, shop  # noqa: F401  (shop is a fixture)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

OK = 'SELECT * FROM "contract"'


class Model:
    """Stands in for the provider; the last reply repeats."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, provider, messages, tools, model, key=""):
        self.calls.append({"messages": messages, "model": model, "key": key, "tools": tools})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"content": text, "tool_calls": []}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()


@pytest.fixture
def me():
    return User.objects.create(username="v@x.com", email="v@x.com", role=Role.VIEWER)


@pytest.fixture
def model(monkeypatch):
    m = Model(OK)
    monkeypatch.setattr(providers, "complete", m)
    return m


def patched_config(monkeypatch, **iq):
    real = siconfig.get_config

    def cfg():
        c = real()
        c["intelligentQuery"].update(iq.get("iq", {}))
        c["chat"].update(iq.get("chat", {}))
        return c

    monkeypatch.setattr(suggest, "get_config", cfg)


def test_reply_cleanup():
    assert suggest.clean_reply("Here:\n```sql\nSELECT 1;\n```\nDone") == "SELECT 1;"
    assert suggest.clean_reply("Sure! Here you go\nselect 2") == "select 2"
    assert suggest.clean_reply("  WITH a AS (SELECT 1)\nSELECT * FROM a  ") == "WITH a AS (SELECT 1)\nSELECT * FROM a"


def test_two_statements_are_rejected(shop, me, model):
    model.replies = ["SELECT 1; SELECT 2"]
    r = suggest.suggest(me, "master", ["x"], "")
    assert r["sql"] is None and "single statement" in r["reason"]


def test_rejects_data_modifying_cte(shop, me, model):
    model.replies = ["WITH d AS (DELETE FROM partner RETURNING *) SELECT * FROM d"]
    r = suggest.suggest(me, "master", ["delete partners"], "")
    assert r["sql"] is None and len(model.calls) == 2
    from schema import conn
    with conn.rw_connection(autocommit=True) as c:
        assert c.execute("SELECT count(*) AS n FROM master.partner").fetchone()["n"] == 2


def test_happy_path(shop, me, model):
    r = suggest.suggest(me, "master", ["signed contract"], "")
    assert r["sql"] == OK and r["source"] == "model" and "contract" in r["tables"] and r["reason"] is None
    assert model.calls[0]["tools"] == []


def test_retry_with_explain_error(shop, me, model):
    build('CREATE TABLE master."sppProject" (id integer PRIMARY KEY)')
    model.replies = ["SELECT * FROM sppProject", 'SELECT * FROM "sppProject"']
    r = suggest.suggest(me, "master", ["projects"], "")
    assert r["sql"] == 'SELECT * FROM "sppProject"'
    assert "does not exist" in model.calls[1]["messages"][-1]["content"]


def test_prompt_has_context(shop, me, model):
    ex = QueryExample.objects.create(
        schema="master", comments="old question", sql=OK, sql_norm=memory.normalize_sql(OK), values=[],
        embedding=fake_vector("signed contract"), status="active")
    suggest.suggest(me, "master", ["signed contract of acme", "and status open"], "SELECT 1 -- so far")
    text = "\n".join(m["content"] for m in model.calls[0]["messages"])
    assert "Main request (newest comment):\nand status open" in text
    assert "signed contract of acme" in text and "SELECT 1 -- so far" in text
    assert 'table "contract"' in text and "Values seen in the data" in text and "Acme Ltd" in text
    assert "old question" in text and ex.sql in text
    assert "SELECT" in model.calls[0]["messages"][0]["content"] and "single" in model.calls[0]["messages"][0]["content"]


def test_uses_browser_key_else_server(shop, me, model):
    suggest.suggest(me, "master", ["a"], "", api_key="k1")
    cache.clear()
    suggest.suggest(me, "master", ["a"], "")
    assert [c["key"] for c in model.calls] == ["k1", ""]


def test_model_setting(shop, me, model, monkeypatch):
    default = siconfig.get_config()["chat"]["defaultModel"]
    suggest.suggest(me, "master", ["a"], "")
    assert model.calls[-1]["model"] == default
    patched_config(monkeypatch, iq={"model": "x/special"})
    cache.clear()
    suggest.suggest(me, "master", ["a"], "")
    assert model.calls[-1]["model"] == "x/special"


def test_rate_limit(shop, me, model):
    for _ in range(20):
        SuggestCall.objects.create(user=me)
    with pytest.raises(suggest.SuggestError) as e:
        suggest.suggest(me, "master", ["a"], "")
    assert e.value.code == "rate_limited" and model.calls == []


def test_cache_still_counts_for_the_rate_limit(shop, me, model):
    suggest.suggest(me, "master", ["signed contract"], "")
    suggest.suggest(me, "master", ["signed contract"], "")
    assert len(model.calls) == 1 and SuggestCall.objects.filter(user=me).count() == 2


def test_not_configured(shop, me, model, monkeypatch):
    patched_config(monkeypatch, iq={"model": ""}, chat={"defaultModel": ""})
    with pytest.raises(suggest.SuggestError) as e:
        suggest.suggest(me, "master", ["a"], "")
    assert e.value.code == "not_configured" and model.calls == []


def test_provider_errors_map(shop, me, monkeypatch):
    def boom(*a, **k):
        raise ChatError("unavailable", "down")
    monkeypatch.setattr(providers, "complete", boom)
    with pytest.raises(suggest.SuggestError) as e:
        suggest.suggest(me, "master", ["a"], "")
    assert e.value.code == "provider_error"


def test_recall_for(shop):
    QueryExample.objects.create(
        schema="master", comments="partner acme", sql='SELECT * FROM "partner"', sql_norm='select * from "partner"',
        values=[{"table": "partner", "column": "name", "value": "Acme Ltd"}],
        embedding=fake_vector("partner acme"), status="active")
    r = suggest.recall_for("master", ["partner acme"])
    assert r["sql"] == 'SELECT * FROM "partner"' and r["source"] == "memory"
    assert suggest.recall_for("master", ["something unrelated entirely"])["sql"] is None
