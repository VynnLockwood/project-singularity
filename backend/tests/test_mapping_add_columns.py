"""scripts.mapping: `add_columns` on a mapping entry whose target table already exists."""
import copy

import pytest

from scripts.mapping import apply_mapping
from scripts.sqlparse import parse_script
from tests.test_script_mapping import EXT, INSERT_SELECT_JOIN, external_state  # noqa: F401 (fixture)

LOAD_MEMO = "INSERT INTO master.item_src (code, name, memo) VALUES ('a', 'Alpha', 'x');"
LOAD = "INSERT INTO master.item_src (code, name) VALUES ('a', 'Alpha'), ('b', 'Beta');"
CREATE_LOAD = ("CREATE TABLE item_src (code text PRIMARY KEY, name text);"
               "INSERT INTO item_src (code, name) VALUES ('a', 'Alpha');")


def mapping(**extra):
    entry = {"target": "item", "columns": {"code": "code", "name": "name"}, **extra}
    return {"tables": {"item_src": entry}}


@pytest.fixture
def state():
    return {"master.item": {"columns": {"item_id": {"name": "item_id", "type": "integer"},
                                        "code": {"name": "code", "type": "text"},
                                        "name": {"name": "name", "type": "text"}},
                            "primary_key": ["item_id"], "unique": [["code"]]}}


def test_add_column_comes_before_the_bulk_insert_and_is_loaded(state):
    m = mapping(add_columns=[{"name": "note", "type": "text", "nullable": True, "comment": "free text"}])
    m["tables"]["item_src"]["columns"]["note"] = "memo"
    res = apply_mapping(parse_script(LOAD_MEMO), m, state)
    assert not res.issues, res.issues
    assert [o["op"] for o in res.ops] == ["add_column", "bulk_insert"]
    assert res.ops[0] == {"op": "add_column", "table": "item",
                          "column": {"name": "note", "type": "text", "nullable": True, "comment": "free text"}}
    assert "note" in res.ops[1]["columns"]


def test_add_columns_on_a_table_the_script_creates_is_an_issue():
    res = apply_mapping(parse_script(CREATE_LOAD), mapping(add_columns=[{"name": "note", "type": "text"}]))
    assert res.ops == []
    assert any("add the column to the table's columns instead" in i.message for i in res.issues)


def test_existing_column_name_is_an_issue(state):
    res = apply_mapping(parse_script(LOAD), mapping(add_columns=[{"name": "name", "type": "text"}]), state)
    assert any("already" in i.message and "name" in i.message for i in res.issues)


def test_duplicate_added_name_is_an_issue(state):
    res = apply_mapping(parse_script(LOAD), mapping(add_columns=[{"name": "x", "type": "text"},
                                                                 {"name": "x", "type": "text"}]), state)
    assert any("more than once" in i.message for i in res.issues)


@pytest.mark.parametrize("col", [{"name": "note", "type": "sql injection"},
                                 {"name": "note", "type": "text; drop"},
                                 {"name": "Note", "type": "text"},
                                 {"name": "no-te", "type": "text"},
                                 {"name": "note"}, {"type": "text"},
                                 {"name": "note", "type": "text", "nullable": "yes"},
                                 {"name": "note", "type": "text", "comment": 3},
                                 {"name": "note", "type": "text", "bogus": 1}, "note"])
def test_bad_column_is_an_issue(state, col):
    res = apply_mapping(parse_script(LOAD), mapping(add_columns=[col]), state)
    assert res.ops == [] and res.issues


def test_add_columns_must_be_a_list(state):
    res = apply_mapping(parse_script(LOAD), mapping(add_columns={"name": "note"}), state)
    assert any("add_columns must be a list" in i.message for i in res.issues)


def test_without_the_key_ops_are_unchanged(external_state):
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), copy.deepcopy(EXT), external_state)
    assert not res.issues
    assert [o["op"] for o in res.ops] == ["bulk_insert"]
    res2 = apply_mapping(parse_script(INSERT_SELECT_JOIN), _with_empty(EXT), external_state)
    assert res2.ops == res.ops


def _with_empty(m):
    m = copy.deepcopy(m)
    m["tables"]["subdistrict_src"]["add_columns"] = []
    return m
