"""Fixes from the whole-branch review: outages, shared memory safety, and value lookup limits."""
import itertools

import psycopg
import pytest
from django.core.cache import cache
from django.test import Client

from accounts.models import Role, User
from chat import providers
from iquery import context, memory, suggest
from iquery.models import QueryExample
from search import embedder
from search.embedder import SearchError

from .test_iquery_api import BASE, login, post  # noqa: F401
from .test_iquery_context import build, shop  # noqa: F401
from .test_iquery_memory import SQL, A, active, user, world  # noqa: F401
from .test_iquery_suggest import OK, Model, patched_config

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()


def test_embedding_outage_does_not_break_suggest_or_recall(shop, monkeypatch):
    me = User.objects.create(username="v@x.com", email="v@x.com", role=Role.VIEWER)
    monkeypatch.setattr(providers, "complete", Model(OK))
    patched_config(monkeypatch, chat={"defaultModel": "m", "provider": "openai"})

    def down(*a, **k):
        raise SearchError("embed is down")
    monkeypatch.setattr(embedder, "embed", down)
    out = suggest.suggest(me, "master", ["contracts"], "")
    assert out["sql"] == OK and out["searchOffline"] is True
    assert suggest.recall_for("master", ["contracts"])["sql"] is None


def test_transient_database_error_keeps_memory(world, monkeypatch):
    ex = active(["a"])

    def down(*a, **k):
        raise psycopg.OperationalError("server closed the connection")
    monkeypatch.setattr(memory, "ro_connection", down)
    assert [e.pk for e in memory.examples("master", ["a"], 3)] == [ex.pk]
    assert QueryExample.objects.filter(pk=ex.pk).exists()


def test_a_missing_column_still_deletes_memory(world):
    ex = active(["a"], sql='SELECT "gone" FROM "partner"')
    assert memory.examples("master", ["a"], 3) == []
    assert not QueryExample.objects.filter(pk=ex.pk).exists()


def test_learn_rejects_sql_that_does_not_plan(world):
    with pytest.raises(ValueError):
        memory.learn(user("a"), "master", ["c"], 'SELECT 1 FROM "no_such_table"', [])
    assert QueryExample.objects.count() == 0


def test_recall_survives_malformed_stored_values(world):
    active(["a"], values=[{"x": 1}])
    assert memory.recall("master", ["a"], A, 0.9) is None


@pytest.mark.parametrize("extra", [
    {"values": [{"x": 1}]}, {"values": "x"}, {"values": [{"table": "t", "column": "c", "value": 1}]},
    {"values": [{"table": "t", "column": "c", "value": "v"}] * 21}, {"sql": "SELECT '" + "x" * 4001 + "'"},
])
def test_learn_endpoint_checks_shape_and_size(world, extra):
    c, _ = login(Role.VIEWER)
    body = {"schema": "master", "comments": ["c"], "sql": SQL, "values": A, **extra}
    assert post(c, "learn", **body).status_code == 400


def test_value_lookup_has_one_overall_deadline_and_keeps_partial_results(embed, monkeypatch):
    build(*[f"CREATE TABLE master.t{i} (t{i}_id bigserial PRIMARY KEY, name text)" for i in (1, 2, 3)],
          *[f"INSERT INTO master.t{i} (name) VALUES ('Acme')" for i in (1, 2, 3)])
    ticks = itertools.count()
    monkeypatch.setattr(context.time, "monotonic", lambda: float(next(ticks)))
    got = context.find_values("master", ["t1", "t2", "t3"], ["Acme"], 2500)
    tables = {v["table"] for v in got}
    assert "t1" in tables and "t3" not in tables


def test_value_lookup_skips_generic_words(embed):
    build("CREATE TABLE master.partner (partner_id bigserial PRIMARY KEY, name text)",
          "INSERT INTO master.partner (name) VALUES ('Aaa Company'), ('Abb Company'), ('Acc Company'), "
          "('Add Company'), ('Zeta Energy')")
    got = context.find_values("master", ["partner"], ["show contracts for company Zeta"], 800)
    assert [v["value"] for v in got] == ["Zeta Energy"]
