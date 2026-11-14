import datetime

import pytest

from accounts.models import Role, User
from iquery import memory
from iquery.models import QueryExample

from .conftest import fake_vector
from .test_iquery_context import build

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("master")]

SQL = 'SELECT * FROM "partner" WHERE "name" = \'Acme Ltd\''
A = [{"table": "partner", "column": "name", "value": "Acme Ltd"}]
B = [{"table": "partner", "column": "name", "value": "Beta Co"}]


@pytest.fixture
def world(embed):
    build("CREATE TABLE master.partner (partner_id bigserial PRIMARY KEY, name text)",
          "INSERT INTO master.partner (name) VALUES ('Acme Ltd')")


def user(name):
    return User.objects.create(username=f"{name}@x.com", email=f"{name}@x.com", role=Role.VIEWER)


def active(comments, sql=SQL, values=A):
    return QueryExample.objects.create(
        schema="master", comments="\n".join(comments[-2:]), sql=sql, sql_norm=memory.normalize_sql(sql),
        values=values, embedding=fake_vector("\n".join(comments[-2:])), status="active")


def test_normalize_keeps_identifiers():
    assert memory.normalize_sql('SELECT  "sppProject".x FROM "sppProject";') == 'select "sppProject".x from "sppProject"'
    assert memory.normalize_sql("SELECT 'A  B'\n;") == "select 'A  B'"


def test_first_learn_is_candidate(world):
    ex = memory.learn(user("a"), "master", ["contracts of acme"], SQL, A)
    assert ex.status == "candidate" and ex.use_count == 1 and len(ex.confirmations) == 1


def test_same_user_same_day_stays_candidate(world):
    u = user("a")
    memory.learn(u, "master", ["contracts of acme"], SQL, A)
    ex = memory.learn(u, "master", ["contracts of acme"], SQL + ";", A)
    assert ex.status == "candidate" and ex.use_count == 2 and QueryExample.objects.count() == 1


def test_other_user_promotes(world):
    memory.learn(user("a"), "master", ["contracts of acme"], SQL, A)
    assert memory.learn(user("b"), "master", ["contracts of acme"], SQL, A).status == "active"


def test_same_user_next_day_promotes(world, monkeypatch):
    u = user("a")
    monkeypatch.setattr(memory, "_today", lambda: datetime.date(2026, 11, 1))
    memory.learn(u, "master", ["contracts of acme"], SQL, A)
    monkeypatch.setattr(memory, "_today", lambda: datetime.date(2026, 11, 2))
    assert memory.learn(u, "master", ["contracts of acme"], SQL, A).status == "active"


def test_different_sql_is_a_new_example(world):
    memory.learn(user("a"), "master", ["contracts of acme"], SQL, A)
    memory.learn(user("b"), "master", ["contracts of acme"], 'SELECT "name" FROM "partner"', A)
    assert QueryExample.objects.count() == 2


def test_learn_refuses_a_non_select(world):
    with pytest.raises(ValueError):
        memory.learn(user("a"), "master", ["x"], "DELETE FROM partner", [])


def test_recall_needs_same_values(world):
    active(["contracts of acme"])
    assert memory.recall("master", ["contracts of acme"], A, 0.95).sql == SQL
    assert memory.recall("master", ["contracts of acme"], B, 0.95) is None


def test_recall_ignores_candidates_and_far_comments(world):
    ex = active(["contracts of acme"])
    assert memory.recall("master", ["something else entirely"], A, 0.95) is None
    ex.status = "candidate"
    ex.save()
    assert memory.recall("master", ["contracts of acme"], A, 0.95) is None


def test_examples_drop_stale(world):
    ok = active(["contracts of acme"])
    gone = active(["contracts of acme again"], sql='SELECT * FROM "dropped_table"')
    got = memory.examples("master", ["contracts of acme"], 3)
    assert [e.pk for e in got] == [ok.pk]
    assert not QueryExample.objects.filter(pk=gone.pk).exists()


def test_explains_rejects_modifying_cte(world):
    err = memory.explains("master", "WITH d AS (DELETE FROM partner RETURNING *) SELECT * FROM d")
    assert err
    build("SELECT 1")
    from schema import conn
    with conn.rw_connection(autocommit=True) as c:
        assert c.execute("SELECT count(*) AS n FROM master.partner").fetchone()["n"] == 1
    assert memory.explains("master", SQL) is None
    assert memory.explains("master", 'SELECT * FROM "nope"')
