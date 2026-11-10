"""State foundation for Plan B: defaults, keys, links, views and enums in the simulated state."""
import copy

import pytest

from gate.state import State, enums_of, views_of
from schema import introspect
from schema.conn import rw_connection

pytestmark = pytest.mark.django_db


@pytest.fixture
def rw(master):
    with rw_connection(autocommit=True) as conn:
        yield conn


def test_snapshot_reports_defaults_keys_and_links(rw):
    rw.execute("CREATE TABLE master.t (t_id int PRIMARY KEY, a text DEFAULT 'x', u text UNIQUE, b int, c int, UNIQUE (b, c))")
    rw.execute("CREATE TABLE master.child (child_id int PRIMARY KEY, t_id int REFERENCES master.t (t_id))")
    snap = introspect.snapshot()
    assert snap["t"]["columns"]["a"]["default"].startswith("'x'")
    assert snap["t"]["columns"]["b"]["default"] is None
    assert snap["t"]["keys"] == {frozenset({"t_id"}), frozenset({"u"}), frozenset({"b", "c"})}
    assert snap["child"]["links"] == {"t"} and snap["t"]["links"] == {"child"}


def test_snapshot_reports_views_and_enums(rw):
    rw.execute("CREATE TABLE master.t (a int)")
    rw.execute("CREATE TYPE master.status AS ENUM ('new', 'done')")
    rw.execute("CREATE VIEW master.v AS SELECT a FROM master.t")
    snap = introspect.snapshot()
    assert isinstance(snap, State)
    assert snap.views["v"]["tables"] == {"t"} and "select" in snap.views["v"]["query"].lower()
    assert snap.enums["status"] == ["new", "done"]
    assert set(snap) == {"t"}                    # views and enums are not tables


def test_state_survives_deepcopy_and_plain_dicts_work():
    st = State({"t": {}})
    st.views["v"] = {"query": "select 1", "tables": set()}
    st.enums["e"] = ["a"]
    cp = copy.deepcopy(st)
    assert views_of(cp) == st.views and enums_of(cp) == {"e": ["a"]}
    assert views_of({}) == {} and enums_of({}) == {}


def test_count_where(rw):
    rw.execute("CREATE TABLE master.t (a text)")
    rw.execute("INSERT INTO master.t VALUES ('x'), ('x'), ('y')")
    assert introspect.count_where("t", {"a": "x"}) == 2
    assert introspect.count_where("t", {"a": ["x", "y"]}) == 3


def test_apply_op_keeps_keys_links_and_defaults_current(master):
    from gate import state as gs
    st = State()
    ops = [
        {"op": "create_table", "name": "p", "columns": [
            {"name": "p_id", "type": "integer", "primary_key": True}, {"name": "code", "type": "text", "unique": True, "default": "z"}]},
        {"op": "create_table", "name": "c", "columns": [
            {"name": "c_id", "type": "integer", "primary_key": True},
            {"name": "p_id", "type": "integer", "references": {"table": "p"}}]},
        {"op": "add_unique", "table": "p", "columns": ["code", "p_id"]},
    ]
    errs, after = gs.validate(ops, st)
    assert errs == []
    assert after["p"]["keys"] == {frozenset({"p_id"}), frozenset({"code"}), frozenset({"code", "p_id"})}
    assert after["p"]["links"] == {"c"} and after["c"]["links"] == {"p"}
    assert after["p"]["columns"]["code"]["default"] == "z"
