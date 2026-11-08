"""scripts.mapping: mapping JSON -> gate ops, including refs to tables that already exist."""
import copy

import pytest

from scripts.mapping import apply_mapping
from scripts.sqlparse import parse_script

INSERT_SELECT_JOIN = (
    "INSERT INTO master.subdistrict_src (code, name, postcode, district_code) "
    "SELECT v.code, v.name, v.postcode, v.district_code "
    "FROM (VALUES ('100101', 'Bang Rak', '10500', '1001')) AS v(code, name, postcode, district_code) "
    "JOIN master.district d ON d.code = v.district_code;"
)

EXT = {"tables": {"subdistrict_src": {
    "target": "subdistrict",
    "columns": {"code": "code", "name": "name", "postcode": "postcode"},
    "refs": [{"column": "district_id", "parent": "master.district", "parent_key": "code",
              "source_column": "district_code", "external": True}]}}}


@pytest.fixture
def external_state():
    """The parent table as it already exists (not created by the script)."""
    return {"master.district": {
        "columns": {"district_id": {"name": "district_id", "type": "integer"},
                    "code": {"name": "code", "type": "text"}},
        "primary_key": ["district_id"], "unique": [["code"]]}}


def test_external_ref_builds_bulk_insert(external_state):
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), EXT, external_state)
    assert not res.issues, res.issues
    bulk = next(o for o in res.ops if o["op"] == "bulk_insert")
    assert bulk["table"] == "subdistrict"
    assert [o["op"] for o in res.ops] == ["bulk_insert"]     # not created by the script: no create_table
    assert bulk["refs"] == [{"column": "district_id", "parent_table": "district",
                             "parent_key": "code", "source_column": "_ref_district_id"}]
    assert bulk["columns"] == ["code", "name", "postcode", "_ref_district_id"]
    assert bulk["source"]["columns"] == ["code", "name", "postcode", "district_code"]
    assert bulk["expected_rows"] == 1


def test_external_ref_false_still_requires_parent_in_script(external_state):
    m = copy.deepcopy(EXT)
    m["tables"]["subdistrict_src"]["refs"][0]["external"] = False
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), m, external_state)
    assert any("not a mapped table" in i.message for i in res.issues)


def test_external_parent_must_exist():
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), EXT)
    assert any("external parent 'master.district' does not exist" in i.message for i in res.issues)


def test_external_parent_key_must_be_a_column(external_state):
    m = copy.deepcopy(EXT)
    m["tables"]["subdistrict_src"]["refs"][0]["parent_key"] = "nope"
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), m, external_state)
    assert any("is not a column of external parent" in i.message for i in res.issues)


def test_external_parent_key_must_be_unique(external_state):
    external_state["master.district"]["unique"] = []
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), EXT, external_state)
    assert any("external ref requires parent_key 'code' to be unique" in i.message for i in res.issues)


def test_a_script_may_touch_a_table_that_already_exists(external_state):
    # CREATE INDEX on master.district: neither created nor loaded by the script, but it exists
    sql = "CREATE INDEX ix_district_code ON master.district (code);"
    res = apply_mapping(parse_script(sql), {"tables": {}}, external_state)
    assert not res.issues, res.issues
    assert apply_mapping(parse_script(sql), {"tables": {}}).issues


def test_ref_shape_still_rejects_a_bad_external_flag():
    m = copy.deepcopy(EXT)
    m["tables"]["subdistrict_src"]["refs"][0]["external"] = "yes"
    res = apply_mapping(parse_script(INSERT_SELECT_JOIN), m, {})
    assert any("each ref must be" in i.message for i in res.issues)
