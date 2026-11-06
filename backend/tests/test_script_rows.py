"""scripts.copyparse (COPY text-format reader) and scripts.mapping (script tables -> target tables + bulk loads)."""
import copy
from pathlib import Path

import pytest

from gate import state as gstate
from gate.operations import OpError, parse_ops
from scripts.copyparse import RowStreamError, iter_copy, iter_copy_rows
from scripts.mapping import MappingResult, apply_mapping, bind_script, suggest_mapping
from scripts.sqlparse import ScriptIssue, parse_script

MINI = (Path(__file__).parent / "data" / "mini_dump.sql").read_text(encoding="utf-8")

MAPPING = {"tables": {
    "province": {"target": "province", "columns": {"code": "province_id", "name": "province_name"}},
    "district": {"target": "district", "columns": {"code": "district_id", "name": "district_name"},
                 "refs": [{"column": "province_id", "parent": "province", "parent_key": "code",
                           "source_column": "province_id"}]},
    "subdistrict": {"target": "subdistrict",
                    "columns": {"code": "subdistrict_id", "name": "subdistrict_name", "postcode": "postcode"},
                    "refs": [{"column": "district_id", "parent": "district", "parent_key": "code",
                              "source_column": "district_id"}]},
}}


def mapped(mapping=None, text=MINI) -> MappingResult:
    p = parse_script(text)
    assert p.errors == []
    return apply_mapping(p, copy.deepcopy(mapping or MAPPING))


def ops_of(res: MappingResult, kind: str) -> list[dict]:
    return [o for o in res.ops if o["op"] == kind]


def created(res: MappingResult, name: str) -> dict:
    return next(o for o in ops_of(res, "create_table") if o["name"] == name)


def bulk(res: MappingResult, table: str) -> dict:
    return next(o for o in ops_of(res, "bulk_insert") if o["table"] == table)


def messages(res: MappingResult) -> str:
    return "\n".join(i.message for i in res.issues)


def gate_ok(ops: list[dict]) -> dict:
    parse_ops(copy.deepcopy(ops), max_ops=500)
    errors, after = gstate.validate(ops, {})
    assert errors == []
    return after


# ---------------------------------------------------------------- COPY reader

def test_backslash_n_is_null():
    assert list(iter_copy("a\t\\N\n\\N\tb\n\\.\n")) == [["a", None], [None, "b"]]


def test_escaped_backslash_n_is_text_not_null():
    assert list(iter_copy("\\\\N\n")) == [["\\N"]]


def test_tab_newline_and_other_escapes():
    block = "a\\tb\tline\\nbreak\tcr\\r\tback\\\\slash\t\\b\\f\\v\n\\.\n"
    assert list(iter_copy(block)) == [["a\tb", "line\nbreak", "cr\r", "back\\slash", "\b\f\v"]]


def test_unknown_escape_is_the_character_itself():
    assert list(iter_copy("\\q\\x\\.z\n")) == [["qx.z"]]


def test_octal_and_hex_escapes_are_utf8_bytes():
    # 'ก' (U+0E01) is e0 b8 81 in UTF-8
    assert list(iter_copy("\\340\\270\\201\t\\xe0\\xb8\\x81\t\\101\\x41\n")) == [["ก", "ก", "AA"]]


def test_invalid_utf8_bytes_raise_with_line():
    with pytest.raises(RowStreamError) as exc:
        list(iter_copy_rows(["ok", "\\377"], first_line=10))
    assert exc.value.issue.line == 11 and "UTF-8" in exc.value.issue.message


def test_nul_byte_escape_rejected():
    with pytest.raises(RowStreamError):
        list(iter_copy("\\000\n"))


def test_thai_text_exact():
    text = "10\tกรุงเทพมหานคร\n11\tสมุทรปราการ\n\\.\n"
    assert list(iter_copy(text)) == [["10", "กรุงเทพมหานคร"], ["11", "สมุทรปราการ"]]


def test_stops_at_terminator_and_handles_missing_final_newline():
    assert list(iter_copy("1\n\\.\n2\n")) == [["1"]]
    assert list(iter_copy("1\n2")) == [["1"], ["2"]]


def test_empty_field_is_empty_string():
    assert list(iter_copy("\t\n")) == [["", ""]]


def test_width_checked():
    with pytest.raises(RowStreamError) as exc:
        list(iter_copy_rows(["a\tb", "c"], width=2, first_line=5))
    assert exc.value.issue.line == 6 and "1 fields, expected 2" in exc.value.issue.message


def test_sqlparse_uses_the_same_reader():
    import scripts.copyparse as cp
    import scripts.sqlparse as sp
    assert sp.iter_copy_rows is cp.iter_copy_rows and sp.RowStreamError is cp.RowStreamError


# ---------------------------------------------------------------- apply_mapping: the mini dump

def test_mapping_mini_dump_has_no_issues_and_passes_the_gate():
    res = mapped()
    assert res.issues == []
    after = gate_ok(res.ops)
    assert set(after) == {"province", "district", "subdistrict"}


def test_create_table_has_surrogate_key_and_mapped_columns():
    res = mapped()
    prov = created(res, "province")
    assert prov["columns"][0] == {"name": "province_id", "type": "integer", "primary_key": True,
                                  "identity": True, "nullable": False}
    assert [c["name"] for c in prov["columns"]] == ["province_id", "code", "name"]
    code = prov["columns"][1]
    assert code["type"] == "varchar(10)" and code["nullable"] is False and "primary_key" not in code
    dist = created(res, "district")
    assert [c["name"] for c in dist["columns"]] == ["district_id", "code", "name", "province_id"]
    fk_col = dist["columns"][3]
    assert fk_col == {"name": "province_id", "type": "integer", "nullable": True}


def test_natural_key_becomes_inline_unique_business_key():
    res = mapped()
    assert ops_of(res, "add_unique") == []
    for t in ("province", "district", "subdistrict"):
        cols = {c["name"]: c for c in created(res, t)["columns"]}
        assert cols["code"]["unique"] is True
        assert [n for n, c in cols.items() if c.get("unique")] == ["code"]
    after = gate_ok(res.ops)
    assert "province_code_key" in after["province"]["constraints"]


def test_multi_column_unique_stays_add_unique():
    text = "CREATE TABLE t (id text PRIMARY KEY, a text, b text);\nALTER TABLE t ADD CONSTRAINT t_ab UNIQUE (a, b);\n"
    res = mapped({"tables": {"t": {"target": "t", "columns": {"code": "id", "a": "a", "b": "b"}}}}, text)
    assert res.issues == []
    assert [o["columns"] for o in ops_of(res, "add_unique")] == [["a", "b"]]
    gate_ok(res.ops)


def test_refs_become_foreign_keys_with_source_on_delete():
    res = mapped()
    fks = {(o["table"], o["column"], o["ref_table"], o["ref_column"], o["on_delete"]) for o in ops_of(res, "add_foreign_key")}
    assert fks == {("district", "province_id", "province", "province_id", "restrict"),
                   ("subdistrict", "district_id", "district", "district_id", "restrict")}


def test_province_code_mapping_yields_bulk_insert_with_refs():
    res = mapped()
    b = bulk(res, "district")
    assert b == {
        "op": "bulk_insert", "table": "district", "columns": ["code", "name", "_ref_province_id"],
        "source": {"script_id": None, "stream": "district",
                   "columns": ["district_id", "district_name", "province_id"]},
        "refs": [{"column": "province_id", "parent_table": "province", "parent_key": "code",
                  "source_column": "_ref_province_id"}],
        "expected_rows": 2,
    }
    assert bulk(res, "province")["refs"] == []


def test_bulk_loads_parent_before_child_and_after_schema_ops():
    res = mapped()
    kinds = [o["op"] for o in res.ops]
    first_bulk = kinds.index("bulk_insert")
    assert all(k == "bulk_insert" for k in kinds[first_bulk:])
    assert [o["table"] for o in ops_of(res, "bulk_insert")] == ["province", "district", "subdistrict"]
    assert [o["name"] for o in ops_of(res, "create_table")] == ["province", "district", "subdistrict"]


def test_expected_rows_counts():
    res = mapped()
    assert {o["table"]: o["expected_rows"] for o in ops_of(res, "bulk_insert")} == {
        "province": 2, "district": 2, "subdistrict": 3}


def test_check_carried_with_renamed_columns():
    m = copy.deepcopy(MAPPING)
    m["tables"]["subdistrict"]["columns"] = {"code": "subdistrict_id", "name": "subdistrict_name", "zip": "postcode"}
    res = mapped(m)
    assert res.issues == []
    (chk,) = ops_of(res, "add_check")
    assert chk["table"] == "subdistrict" and chk["name"] == "chk_subdistrict_postcode"
    assert chk["expression"] == "CAST(zip AS text) ~ CAST('^[0-9]{5}$' AS text)"
    gate_ok(res.ops)


def test_check_rewrite_only_touches_column_refs():
    text = ("CREATE TABLE t (id integer PRIMARY KEY, a text, b text,"
            " CONSTRAINT c1 CHECK (a <> 'a' AND lower(b) = 'b' AND a <> b));\n")
    m = {"tables": {"t": {"target": "t", "columns": {"code": "id", "x": "a", "a": "b"}}}}
    res = mapped(m, text)
    assert res.issues == []
    (chk,) = ops_of(res, "add_check")
    assert chk["expression"] == "x <> 'a' AND lower(a) = 'b' AND x <> a"


def test_indexes_follow_columns_and_refs():
    res = mapped()
    idx = {(o["table"], tuple(o["columns"])) for o in ops_of(res, "add_index")}
    assert idx == {("district", ("province_id",)), ("subdistrict", ("district_id",))}


def test_target_names_can_differ_from_source():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = "geo_province"
    res = mapped(m)
    assert res.issues == []
    assert created(res, "geo_province")["columns"][0]["name"] == "geo_province_id"
    b = bulk(res, "district")
    assert b["refs"][0]["parent_table"] == "geo_province"
    fk = next(o for o in ops_of(res, "add_foreign_key") if o["table"] == "district")
    assert (fk["ref_table"], fk["ref_column"]) == ("geo_province", "geo_province_id")
    gate_ok(res.ops)


def test_comments_carried_to_target_names():
    text = ("CREATE TABLE t (id integer PRIMARY KEY, a text);\n"
            "COMMENT ON TABLE t IS 'the t';\nCOMMENT ON COLUMN t.a IS 'the a';\n")
    res = mapped({"tables": {"t": {"target": "tee", "columns": {"code": "id", "aa": "a"}}}}, text)
    assert res.issues == []
    t = created(res, "tee")
    assert t["comment"] == "the t"
    assert next(c for c in t["columns"] if c["name"] == "aa")["comment"] == "the a"
    gate_ok(res.ops)


def test_table_without_rows_gets_no_bulk_insert():
    res = mapped({"tables": {"t": {"target": "t", "columns": {"code": "id"}}}}, "CREATE TABLE t (id integer PRIMARY KEY);\n")
    assert res.issues == [] and ops_of(res, "bulk_insert") == []


def test_plain_fk_column_kept_references_parent_mapped_column():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"] = {"target": "district",
                               "columns": {"code": "district_id", "name": "district_name", "province_code": "province_id"}}
    res = mapped(m)
    assert res.issues == []
    fk = next(o for o in ops_of(res, "add_foreign_key") if o["table"] == "district")
    assert (fk["column"], fk["ref_table"], fk["ref_column"], fk["on_delete"]) == ("province_code", "province", "code", "restrict")
    assert bulk(res, "district")["refs"] == []
    names = [o["table"] for o in ops_of(res, "bulk_insert")]
    assert names.index("province") < names.index("district")
    gate_ok(res.ops)


def test_stream_only_table_loads_into_existing_table():
    text = "COPY t (a, b) FROM stdin;\n1\tx\n2\ty\n\\.\n"
    res = mapped({"tables": {"t": {"target": "things", "columns": {"num": "a", "label": "b"}}}}, text)
    assert res.issues == []
    assert res.ops == [{"op": "bulk_insert", "table": "things", "columns": ["num", "label"],
                        "source": {"script_id": None, "stream": "t", "columns": ["a", "b"]},
                        "refs": [], "expected_rows": 2}]


def test_bind_script_sets_script_id_without_mutating():
    res = mapped()
    bound = bind_script(res.ops, 42)
    assert all(o["source"]["script_id"] == 42 for o in bound if o["op"] == "bulk_insert")
    assert all(o["source"]["script_id"] is None for o in res.ops if o["op"] == "bulk_insert")


# ---------------------------------------------------------------- apply_mapping: issues

def issue_for(mapping, text=MINI) -> str:
    res = mapped(mapping, text)
    assert res.issues, "expected an issue"
    assert all(isinstance(i, ScriptIssue) for i in res.issues)
    return messages(res)


def test_unknown_source_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["columns"]["name"] = "province_nme"
    msg = issue_for(m)
    assert "province_nme" in msg and "does not exist" in msg


def test_unknown_source_table_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["provinces"] = m["tables"].pop("province")
    msg = issue_for(m)
    assert "provinces" in msg


@pytest.mark.parametrize("bad", ["Province", "provinceName", "1province", "pro-vince", "a" * 64, "pro__vince", "x_"])
def test_target_table_name_not_snake_case_is_an_issue(bad):
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = bad
    assert "snake_case" in issue_for(m)


def test_target_column_name_not_snake_case_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["columns"] = {"code": "province_id", "provinceName": "province_name"}
    assert "provinceName" in issue_for(m)


def test_ref_column_not_snake_case_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"][0]["column"] = "ProvinceId"
    assert "ProvinceId" in issue_for(m)


def test_surrogate_name_too_long_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = "p" * 62
    assert "63" in issue_for(m)


def test_duplicate_target_table_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["target"] = "province"
    assert "more than one" in issue_for(m)


def test_ref_column_duplicating_mapped_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["columns"]["province_id"] = "district_name"
    assert "province_id" in issue_for(m)


def test_mapped_column_named_like_surrogate_key_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["columns"] = {"province_id": "province_id", "name": "province_name"}
    assert "reserved" in issue_for(m)


def test_ref_to_unmapped_parent_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"][0]["parent"] = "region"
    assert "region" in issue_for(m)


def test_parent_key_not_a_parent_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"][0]["parent_key"] = "province_code"
    assert "province_code" in issue_for(m)


def test_parent_key_not_unique_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"][0]["parent_key"] = "name"
    assert "unique" in issue_for(m)


def test_ref_source_column_unknown_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"][0]["source_column"] = "prov"
    assert "prov" in issue_for(m)


def test_ref_disagreeing_with_source_fk_is_an_issue():
    m = copy.deepcopy(MAPPING)
    # district.province_id references province in the script, not subdistrict
    m["tables"]["district"]["refs"][0]["parent"] = "subdistrict"
    assert "references province" in issue_for(m)


def test_dropping_a_check_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["subdistrict"]["columns"].pop("postcode")
    m["tables"]["subdistrict"]["drop"] = ["postcode"]
    msg = issue_for(m)
    assert "chk_subdistrict_postcode" in msg and "postcode" in msg


def test_dropping_an_index_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"] = []
    m["tables"]["district"]["drop"] = ["province_id"]
    m["tables"]["subdistrict"]["refs"] = []
    m["tables"]["subdistrict"]["drop"] = ["district_id"]
    msg = issue_for(m)
    assert "idx_district_province_id" in msg and "fk_district_province" in msg


def test_dropping_the_natural_key_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["columns"].pop("code")
    m["tables"]["province"]["drop"] = ["province_id"]
    m["tables"]["district"]["refs"] = []
    m["tables"]["district"]["columns"]["province_code"] = "province_id"
    assert "primary key" in issue_for(m)


def test_unaccounted_source_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["columns"].pop("name")
    msg = issue_for(m)
    assert "province_name" in msg and "drop" in msg


def test_drop_of_unknown_column_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["drop"] = ["nope"]
    assert "nope" in issue_for(m)


def test_ref_source_column_in_check_is_an_issue():
    text = ("CREATE TABLE p (id text PRIMARY KEY);\n"
            "CREATE TABLE c (id text PRIMARY KEY, pid text REFERENCES p(id), CONSTRAINT ck CHECK (pid <> 'x'));\n")
    m = {"tables": {"p": {"target": "p", "columns": {"code": "id"}},
                    "c": {"target": "c", "columns": {"code": "id"},
                          "refs": [{"column": "p_id", "parent": "p", "parent_key": "code", "source_column": "pid"}]}}}
    assert "ck" in issue_for(m, text)


def test_missing_mapping_for_streamed_table_is_an_issue():
    m = copy.deepcopy(MAPPING)
    del m["tables"]["subdistrict"]
    assert "subdistrict" in issue_for(m)


def test_cyclic_refs_are_an_issue():
    text = ("CREATE TABLE a (id text PRIMARY KEY, bid text);\n"
            "CREATE TABLE b (id text PRIMARY KEY, aid text REFERENCES a(id));\n"
            "ALTER TABLE a ADD CONSTRAINT a_b FOREIGN KEY (bid) REFERENCES b(id);\n"
            "COPY a (id, bid) FROM stdin;\n1\t1\n\\.\nCOPY b (id, aid) FROM stdin;\n1\t1\n\\.\n")
    m = {"tables": {
        "a": {"target": "a", "columns": {"code": "id"},
              "refs": [{"column": "b_id", "parent": "b", "parent_key": "code", "source_column": "bid"}]},
        "b": {"target": "b", "columns": {"code": "id"},
              "refs": [{"column": "a_id", "parent": "a", "parent_key": "code", "source_column": "aid"}]}}}
    assert "cycl" in issue_for(m, text)


def test_self_ref_is_an_issue():
    text = "CREATE TABLE a (id text PRIMARY KEY, parent text REFERENCES a(id));\n"
    m = {"tables": {"a": {"target": "a", "columns": {"code": "id"},
                          "refs": [{"column": "parent_id", "parent": "a", "parent_key": "code", "source_column": "parent"}]}}}
    assert "itself" in issue_for(m, text)


@pytest.mark.parametrize("bad", [
    None, [], {"tables": []}, {"tables": {"province": "x"}}, {"tables": {}, "extra": 1},
    {"tables": {"province": {"target": "province", "columns": {"code": 5}}}},
    {"tables": {"province": {"target": "province", "columns": {}, "bogus": 1}}},
    {"tables": {"province": {"target": "province", "columns": {}, "refs": [{"column": "x"}]}}},
    {"tables": {"province": {"target": "province", "columns": {}, "drop": "province_id"}}},
])
def test_malformed_mapping_is_an_issue_not_an_exception(bad):
    res = apply_mapping(parse_script(MINI), bad)
    assert res.issues and res.ops == []


def test_row_stream_error_reported_while_counting():
    text = ("CREATE TABLE t (id text PRIMARY KEY, a text);\n"
            "COPY t (id, a) FROM stdin;\n1\tx\n2\ty\tz\n\\.\n")
    res = mapped({"tables": {"t": {"target": "t", "columns": {"code": "id", "a": "a"}}}}, text)
    (iss,) = res.issues
    assert iss.line == 4 and "3 fields, expected 2" in iss.message
    assert ops_of(res, "bulk_insert") == []


def test_issues_mean_no_ops():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = "Province"
    assert mapped(m).ops == []


# ---------------------------------------------------------------- suggest_mapping

def test_suggest_mapping_mini_dump():
    p = parse_script(MINI)
    m = suggest_mapping(p)
    assert m == {"tables": {
        "province": {"target": "province", "columns": {"code": "province_id", "province_name": "province_name"},
                     "drop": [], "refs": []},
        "district": {"target": "district", "columns": {"code": "district_id", "district_name": "district_name"},
                     "drop": [], "refs": [{"column": "province_id", "parent": "province", "parent_key": "code",
                                           "source_column": "province_id"}]},
        "subdistrict": {"target": "subdistrict",
                        "columns": {"code": "subdistrict_id", "subdistrict_name": "subdistrict_name",
                                    "postcode": "postcode"},
                        "drop": [], "refs": [{"column": "district_id", "parent": "district", "parent_key": "code",
                                              "source_column": "district_id"}]},
    }}


def test_suggest_then_apply_round_trip_passes_gate():
    p = parse_script(MINI)
    res = apply_mapping(p, suggest_mapping(p))
    assert res.issues == []
    creates = [o for o in res.ops if o["op"] == "create_table"]
    after = gate_ok(creates)
    assert set(after) == {"province", "district", "subdistrict"}
    gate_ok(res.ops)
    parse_ops(bind_script(res.ops, 1))


def test_suggest_snake_cases_odd_names_and_round_trips():
    text = ('CREATE TABLE "Shop-Items" ("ItemCode" text PRIMARY KEY, "Item-Name" text, "itemName" text);\n'
            'COPY "Shop-Items" ("ItemCode", "Item-Name", "itemName") FROM stdin;\nA\tx\ty\n\\.\n')
    p = parse_script(text)
    assert p.errors == []
    m = suggest_mapping(p)
    assert m["tables"]["Shop-Items"]["target"] == "shop_items"
    assert m["tables"]["Shop-Items"]["columns"] == {"itemcode": "ItemCode", "item_name": "Item-Name",
                                                    "itemname": "itemName"}
    res = apply_mapping(p, m)
    assert res.issues == []
    assert bulk(res, "shop_items")["columns"] == ["itemcode", "item_name", "itemname"]
    gate_ok(res.ops)


def test_suggest_handles_code_named_key_and_name_collisions():
    text = ("CREATE TABLE \"Region\" (region_code text PRIMARY KEY, \"Label\" text, label text);\n"
            "CREATE TABLE town (id integer PRIMARY KEY, region_code text REFERENCES \"Region\"(region_code));\n"
            "COPY \"Region\" (region_code, \"Label\", label) FROM stdin;\nN\ta\tb\n\\.\n"
            "COPY town (id, region_code) FROM stdin;\n1\tN\n\\.\n")
    p = parse_script(text)
    assert p.errors == []
    m = suggest_mapping(p)
    reg = m["tables"]["Region"]
    assert reg["target"] == "region"
    assert reg["columns"]["code"] == "region_code"
    assert sorted(reg["columns"].values()) == ["Label", "label", "region_code"]
    assert m["tables"]["town"]["refs"] == [{"column": "region_id", "parent": "Region", "parent_key": "code",
                                           "source_column": "region_code"}]
    res = apply_mapping(p, m)
    assert res.issues == []
    gate_ok(res.ops)


# ---------------------------------------------------------------- op shape (gate.operations)

def _bulk_op(**src):
    return {"op": "bulk_insert", "table": "t", "columns": ["a", "b"],
            "source": {"script_id": 1, "stream": "s", "columns": ["x", "y"], **src}, "expected_rows": 1}


def test_shape_accepts_source_columns_and_null_script_id():
    parse_ops([_bulk_op()])
    parse_ops([_bulk_op(script_id=None)])


@pytest.mark.parametrize("src", [{"columns": ["x"]}, {"columns": "x"}, {"columns": ["x", 1]}, {"script_id": "1"},
                                 {"columns": ["x", "bad name"]}])
def test_shape_rejects_bad_source(src):
    with pytest.raises(OpError):
        parse_ops([_bulk_op(**src)])


def test_shape_requires_source_columns():
    op = _bulk_op()
    del op["source"]["columns"]
    with pytest.raises(OpError):
        parse_ops([op])


def test_single_column_uniques_become_one_inline_unique_each():
    text = ("CREATE TABLE t (id text PRIMARY KEY, a text UNIQUE, b text, c text);\n"
            "ALTER TABLE t ADD CONSTRAINT t_b_key UNIQUE (b);\nALTER TABLE t ADD CONSTRAINT t_b_key2 UNIQUE (b);\n")
    res = mapped({"tables": {"t": {"target": "t", "columns": {"code": "id", "aa": "a", "bb": "b", "cc": "c"}}}}, text)
    assert res.issues == []
    assert ops_of(res, "add_unique") == []
    assert {c["name"]: c.get("unique", False) for c in created(res, "t")["columns"]} == {
        "t_id": False, "code": True, "aa": True, "bb": True, "cc": False}
    gate_ok(res.ops)


def test_ref_to_wrong_parent_reported_once():
    m = copy.deepcopy(MAPPING)
    m["tables"]["district"]["refs"][0]["parent"] = "subdistrict"
    res = mapped(m)
    assert not any("fk_district_province" in i.message for i in res.issues)


def test_one_source_column_mapped_twice_is_an_issue():
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["columns"]["label"] = "province_name"
    msg = issue_for(m)
    assert "more than one target column" in msg and "province_name" in msg


def test_ref_source_column_without_row_data_is_an_issue():
    text = ("CREATE TABLE p (id text PRIMARY KEY);\n"
            "CREATE TABLE c (id text PRIMARY KEY, pid text REFERENCES p(id));\n"
            "COPY p (id) FROM stdin;\nA\n\\.\nCOPY c (id) FROM stdin;\n1\n\\.\n")
    m = {"tables": {"p": {"target": "p", "columns": {"code": "id"}},
                    "c": {"target": "c", "columns": {"code": "id"},
                          "refs": [{"column": "p_id", "parent": "p", "parent_key": "code", "source_column": "pid"}]}}}
    res = mapped(m, text)
    (iss,) = res.issues
    assert iss.statement == "tables.c.refs[0]"
    assert "'p_id'" in iss.message and "'pid'" in iss.message and "columns: id" in iss.message
    assert res.ops == []


def test_rows_not_read_when_mapping_has_issues():
    p = parse_script(MINI)
    reads = []
    for st in p.streams.values():
        orig = st.rows
        st.rows = lambda orig=orig, name=st.table: (reads.append(name), orig())[1]
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = "Province"
    res = apply_mapping(p, m)
    assert res.issues and reads == []
    apply_mapping(p, copy.deepcopy(MAPPING))
    assert sorted(reads) == ["district", "province", "subdistrict"]


def test_mapped_mini_dump_has_no_business_key_warning():
    from gate import check
    from gate.defaults import DEFAULT_RULES
    rules = [{**r, "enabled": True} for r in DEFAULT_RULES]
    res = mapped()
    out = check.run(bind_script(res.ops, 1), {}, rules, actor="me@example.com", allow_bulk=True, max_script_rows=100,
                    max_ops=100)
    assert res.ops and not [v for v in out.violations if "business key" in str(v)], out.violations


@pytest.mark.usefixtures("master")
def test_mapped_mini_dump_loads_end_to_end():
    from gate import check
    from gate.defaults import DEFAULT_RULES
    from schema import executor, introspect, query

    rules = [{**r, "enabled": True} for r in DEFAULT_RULES]
    p = parse_script(MINI)
    ops = bind_script(apply_mapping(p, suggest_mapping(p)).ops, 1)
    st = introspect.snapshot()
    res = check.run(ops, st, rules, actor="me@example.com", allow_bulk=True, max_script_rows=100, max_ops=100)
    assert res.ok and not res.violations, (res.errors, res.violations)

    def row_source(op):  # what Task 9's provider does: project stream rows into op column order
        stream = p.streams[op["source"]["stream"]]
        idx = [stream.columns.index(c) for c in op["source"]["columns"]]
        return (tuple(r[i] for i in idx) for r in stream.rows())

    executor.execute(res.operations, st, "me@example.com", rules=rules, row_source=row_source)

    def q(sql):
        r = query.run_select(sql)
        names = [c["name"] if isinstance(c, dict) else c for c in r["columns"]]
        return [dict(zip(names, x)) for x in r["rows"]]

    got = q("SELECT s.code AS s, s.subdistrict_name AS n, d.code AS d, p.code AS p, p.province_name AS pn "
            "FROM master.subdistrict s JOIN master.district d ON d.district_id = s.district_id "
            "JOIN master.province p ON p.province_id = d.province_id ORDER BY s.code")
    assert [(r["s"], r["n"], r["d"], r["p"], r["pn"]) for r in got] == [
        ("100101", "พระบรมมหาราชวัง", "1001", "10", "กรุงเทพมหานคร"),
        ("100102", "วังบูรพาภิรมย์", "1001", "10", "กรุงเทพมหานคร"),
        ("110101", "ปากน้ำ", "1101", "11", "สมุทรปราการ")]
    cons = q("SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
             "WHERE n.nspname = 'master' AND contype = 'u' ORDER BY 1")
    assert [r["conname"] for r in cons] == ["district_code_key", "province_code_key", "subdistrict_code_key"]
