"""Task 8: validation report with dry run (scripts.validate) and the bulk row provider (scripts.rowsource)."""
import copy
import json
import os

import pytest

from accounts.models import Role, User
from schema import executor, introspect, query
from scripts import storage
from scripts.mapping import suggest_mapping
from scripts.models import Script
from scripts.rowsource import make_row_source
from scripts.sqlparse import parse_script
from scripts.validate import validate
from settings_app import services as rules_service

pytestmark = pytest.mark.django_db

MINI_PATH = os.path.join(os.path.dirname(__file__), "data", "mini_dump.sql")
with open(MINI_PATH, encoding="utf-8") as _f:
    MINI = _f.read()
MAPPING = suggest_mapping(parse_script(MINI))


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    return settings.SCRIPT_DIR


@pytest.fixture
def owner():
    return User.objects.create(username="ed@x.io", email="ed@x.io", role=Role.EDITOR)


@pytest.fixture
def approver():
    return User.objects.create(username="ap@x.io", email="ap@x.io", role=Role.APPROVER)


def stored(owner, text: str) -> Script:
    return storage.save(owner, "dump.sql", text.encode("utf-8"))


def tables():
    return sorted(t["name"] for t in introspect.list_tables())


def tbl(report, name):
    return next(t for t in report["tables"] if t["name"] == name)


# ---------------------------------------------------------------- validate (needs master)

@pytest.mark.usefixtures("master")
def test_clean_mini_dump_is_ok_and_leaves_no_trace(owner):
    s = stored(owner, MINI)
    r = validate(s, copy.deepcopy(MAPPING))
    assert r["ok"], r
    assert r["mapping_used"] == "given" and r["errors"] == [] and r["gate"]["blocked"] == []
    assert {t["name"]: t["rows"] for t in r["tables"]} == {"province": 2, "district": 2, "subdistrict": 3}
    assert all(t["orphans"] == 0 and t["sample_orphans"] == [] for t in r["tables"])
    assert r["dry_run"] == {"ok": True, "skipped": False, "error": None,
                            "rows_loaded": {"province": 2, "district": 2, "subdistrict": 3}}
    assert r["statements"] > 0 and r["ignored"] and {"line", "statement"} <= set(r["ignored"][0])
    assert r["ops_summary"]["by_op"]["bulk_insert"] == 3 and r["ops_summary"]["by_op"]["create_table"] == 3
    assert r["ops_summary"]["tables"] == ["district", "province", "subdistrict"]
    assert "operations" not in r
    assert tables() == []  # dry run rolled back
    s.refresh_from_db()
    assert s.status == "validated" and s.report == json.loads(json.dumps(r)) and s.mapping == MAPPING


@pytest.mark.usefixtures("master")
def test_mapping_none_uses_suggestion(owner):
    s = stored(owner, MINI)
    r = validate(s, None)
    assert r["ok"] and r["mapping_used"] == "suggested"
    s.refresh_from_db()
    assert s.mapping == MAPPING and s.status == "validated"


@pytest.mark.usefixtures("master")
def test_orphan_subdistrict_reported_and_db_untouched(owner):
    bad = MINI.replace("110101\tปากน้ำ\t10270\t1101", "110101\tปากน้ำ\t10270\t9999")
    s = stored(owner, bad)
    r = validate(s, None)
    assert not r["ok"]
    sub = tbl(r, "subdistrict")
    assert sub["orphans"] == 1 and tbl(r, "district")["orphans"] == 0
    (o,) = sub["sample_orphans"]
    line = bad.splitlines().index("110101\tปากน้ำ\t10270\t9999") + 1
    assert o == {"line": line, "row": 3, "column": "district_id", "value": "9999", "parent": "district.code"}
    assert r["dry_run"]["ok"] is False and r["dry_run"]["skipped"] is True
    assert tables() == []
    s.refresh_from_db()
    assert s.status == "uploaded" and s.report["tables"]


@pytest.mark.usefixtures("master")
def test_null_and_empty_keys_are_not_orphans(owner):
    text = MINI.replace("110101\tปากน้ำ\t10270\t1101", "110101\tปากน้ำ\t10270\t\\N")
    text = text.replace("100102\tวังบูรพาภิรมย์\t10200\t1001", "100102\tวังบูรพาภิรมย์\t10200\t")
    r = validate(stored(owner, text), None)
    assert tbl(r, "subdistrict")["orphans"] == 0 and r["ok"], r


@pytest.mark.usefixtures("master")
def test_check_violation_fails_dry_run_and_db_unchanged(owner):
    bad = MINI.replace("110101\tปากน้ำ\t10270\t1101", "110101\tปากน้ำ\t1027X\t1101")
    s = stored(owner, bad)
    r = validate(s, None)
    assert not r["ok"] and r["errors"] == [] and r["gate"]["blocked"] == []
    err = r["dry_run"]["error"]
    assert r["dry_run"]["ok"] is False and r["dry_run"]["skipped"] is False
    assert "subdistrict" in err and "chk_subdistrict_postcode" in err and "1027X" in err  # table and row
    assert r["dry_run"]["rows_loaded"] == {}
    assert tables() == []
    s.refresh_from_db()
    assert s.status == "uploaded"


@pytest.mark.usefixtures("master")
def test_blocking_gate_rule_listed_and_no_dry_run(owner, approver):
    rules_service.create_rule(approver, name="postcodeIsText", type="type_map",
                              params={"columnPattern": "^postcode$", "allowedTypes": ["text"]}, severity="block")
    s = stored(owner, MINI)
    r = validate(s, None)
    assert not r["ok"]
    assert [v["rule"] for v in r["gate"]["blocked"]] == ["postcodeIsText"]
    assert r["dry_run"]["skipped"] is True and tables() == []
    s.refresh_from_db()
    assert s.status == "uploaded"


@pytest.mark.usefixtures("master")
def test_existing_table_is_a_gate_error(owner, approver):
    from approvals import service
    c = service.propose(approver, [{"op": "create_table", "name": "province",
                                    "columns": [{"name": "code", "type": "text", "unique": True}]}], source="web")
    assert service.approve(approver, c.pk).status == "applied"
    r = validate(stored(owner, MINI), None)
    assert not r["ok"] and r["gate"]["errors"] and any("province" in e for e in r["gate"]["errors"])
    assert r["dry_run"]["skipped"] is True


@pytest.mark.usefixtures("master")
def test_camel_case_target_is_a_mapping_error(owner):
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = "provinceList"
    s = stored(owner, MINI)
    r = validate(s, m)
    assert not r["ok"]
    (e,) = r["errors"]
    assert e["kind"] == "mapping" and e["statement"] == "tables.province.target" and "provinceList" in e["message"]
    assert r["tables"] == [] and r["dry_run"]["skipped"] is True and r["gate"]["blocked"] == []
    s.refresh_from_db()
    assert s.mapping == m and s.status == "uploaded"


@pytest.mark.usefixtures("master")
def test_revalidate_flips_status_both_ways(owner, approver):
    m = copy.deepcopy(MAPPING)
    m["tables"]["province"]["target"] = "provinceList"
    s = stored(owner, MINI)
    assert not validate(s, m)["ok"]
    s.refresh_from_db()
    assert s.status == "uploaded"
    assert validate(s, None)["ok"]
    s.refresh_from_db()
    assert s.status == "validated"
    rules_service.create_rule(approver, name="postcodeIsText", type="type_map",
                              params={"columnPattern": "^postcode$", "allowedTypes": ["text"]}, severity="block")
    assert not validate(s, None)["ok"]
    s.refresh_from_db()
    assert s.status == "validated"  # review ruling 9: a failing re-validate never downgrades


@pytest.mark.usefixtures("master")
def test_parse_errors_reported_and_stop(owner):
    s = stored(owner, "CREATE TABLE a (id integer);\nDROP TABLE b;\n")
    r = validate(s, None)
    assert not r["ok"] and r["statements"] == 2
    (e,) = r["errors"]
    assert e["kind"] == "parse" and e["line"] == 2 and "DROP" in e["statement"]
    assert r["mapping_used"] is None and r["tables"] == [] and r["dry_run"]["skipped"] is True
    s.refresh_from_db()
    assert s.status == "uploaded" and s.report == r


@pytest.mark.usefixtures("master")
def test_bad_copy_row_is_a_rows_error(owner):
    bad = MINI.replace("10\tกรุงเทพมหานคร\n", "10\tกรุงเทพมหานคร\textra\n")
    r = validate(stored(owner, bad), None)
    assert not r["ok"]
    (e,) = r["errors"]
    assert e["line"] == bad.splitlines().index("10\tกรุงเทพมหานคร\textra") + 1 and "fields" in e["message"]


@pytest.mark.usefixtures("master")
def test_missing_file_is_a_storage_error(owner, script_dir):
    s = stored(owner, MINI)
    os.unlink(os.path.join(script_dir, s.path))
    r = validate(s, None)
    assert not r["ok"] and r["errors"][0]["kind"] == "storage" and "missing" in r["errors"][0]["message"]


@pytest.mark.usefixtures("master")
def test_report_is_bounded(owner):
    lines = ["CREATE TABLE p (code varchar(10) PRIMARY KEY, name text);",
             "CREATE TABLE c (code varchar(10) PRIMARY KEY, p_code varchar(10) REFERENCES p(code));",
             "COPY p (code, name) FROM stdin;", "P1\tone", "\\.",
             "COPY c (code, p_code) FROM stdin;"]
    lines += [f"C{i}\tX{i}" for i in range(5000)] + ["\\."]
    r = validate(stored(owner, "\n".join(lines) + "\n"), None)
    c = tbl(r, "c")
    assert c["rows"] == 5000 and c["orphans"] == 5000 and len(c["sample_orphans"]) == 10
    assert c["sample_orphans"][0] == {"line": 7, "row": 1, "column": "p_code", "value": "X0", "parent": "p.code"}
    assert len(json.dumps(r)) < 20_000


@pytest.mark.usefixtures("master")
def test_error_and_ignored_lists_are_capped(owner):
    text = "".join(f"DROP TABLE t{i};\nSET a = {i};\n" for i in range(300))
    r = validate(stored(owner, text), None)
    assert len(r["errors"]) == 200 and r["errors_truncated"] == 100
    assert len(r["ignored"]) == 100 and r["ignored_truncated"] == 200
    assert len(json.dumps(r)) < 100_000


# ---------------------------------------------------------------- make_row_source

def bulk_op(script_id, stream="district", columns=("district_name", "district_id", "province_id")):
    return {"op": "bulk_insert", "table": "district", "columns": [f"c{i}" for i in range(len(columns))],
            "source": {"script_id": script_id, "stream": stream, "columns": list(columns)}, "refs": [],
            "expected_rows": 2}


def test_row_source_projects_in_source_column_order(owner):
    s = stored(owner, MINI)
    rs = make_row_source(s)
    it = rs(bulk_op(s.id))
    assert not isinstance(it, (list, tuple))
    assert list(it) == [("พระนคร", "1001", "10"), ("เมืองสมุทรปราการ", "1101", "11")]
    # a source column may feed two op columns; another stream from the same provider
    assert list(rs(bulk_op(s.id, "province", ("province_id", "province_id")))) == [("10", "10"), ("11", "11")]


def test_row_source_missing_column_or_stream(owner):
    s = stored(owner, MINI)
    rs = make_row_source(s)
    with pytest.raises(executor.ApplyError, match="no column 'nope'.*district_id"):
        list(rs(bulk_op(s.id, columns=("district_id", "nope"))))
    with pytest.raises(executor.ApplyError, match="no rows for 'ghost'"):
        list(rs(bulk_op(s.id, stream="ghost")))
    with pytest.raises(executor.ApplyError, match="script"):
        list(rs(bulk_op(s.id + 1)))


def test_row_source_bad_copy_row_names_the_line(owner):
    bad = MINI.replace("1101\tเมืองสมุทรปราการ\t11\n", "1101\tเมืองสมุทรปราการ\n")
    s = stored(owner, bad)
    line = bad.splitlines().index("1101\tเมืองสมุทรปราการ") + 1
    it = make_row_source(s)(bulk_op(s.id))
    assert next(it) == ("พระนคร", "1001", "10")
    with pytest.raises(executor.ApplyError, match=f"script line {line}: .*district.*2 fields, expected 3"):
        next(it)


def test_row_source_tampered_file(owner, script_dir):
    s = stored(owner, MINI)
    with open(os.path.join(script_dir, s.path), "ab") as f:
        f.write(b"-- x\n")
    with pytest.raises(executor.ApplyError, match="checksum"):
        list(make_row_source(s)(bulk_op(s.id)))


def test_numbered_rows_lines():
    p = parse_script("CREATE TABLE a (x int);\nINSERT INTO a VALUES (1), (2);\nCOPY a (x) FROM stdin;\n3\n4\n\\.\n")
    assert list(p.streams["a"].numbered_rows()) == [(2, ["1"]), (2, ["2"]), (4, ["3"]), (5, ["4"])]
    assert p.statements == 3


@pytest.mark.usefixtures("master")
def test_row_source_error_inside_executor_rolls_back(owner, script_dir):
    """A row-source failure raised inside the executor's COPY surfaces as ApplyError and leaves nothing."""
    from gate import check
    from scripts.mapping import apply_mapping, bind_script
    s = stored(owner, MINI)
    p = parse_script(MINI)
    ops = bind_script(apply_mapping(p, suggest_mapping(p)).ops, s.id)
    st = introspect.snapshot()
    rules = rules_service.active_rules()
    res = check.run(ops, st, rules, actor=owner.email, allow_bulk=True, max_ops=100)
    assert res.ok, (res.errors, res.violations)
    with open(os.path.join(script_dir, s.path), "ab") as f:
        f.write(b"-- tampered\n")
    for dry in (True, False):
        with pytest.raises(executor.ApplyError, match="checksum"):
            executor.execute(res.operations, st, owner.email, rules=rules, row_source=make_row_source(s), dry_run=dry)
        assert tables() == []


# ---------------------------------------------------------------- pending fixes (Task 8 review)

@pytest.mark.usefixtures("master")
def test_submitted_or_expired_script_is_refused(owner):
    for status in ("submitted", "expired"):
        s = stored(owner, MINI)
        s.status = status
        s.save(update_fields=["status"])
        r = validate(s, copy.deepcopy(MAPPING))
        assert not r["ok"] and r["errors"]
        assert r["errors"][0]["kind"] == "storage" and status in r["errors"][0]["message"]
        s.refresh_from_db()
        assert s.status == status  # untouched
        s.delete()


@pytest.mark.usefixtures("master")
def test_failed_revalidate_leaves_validated_status(owner, approver):
    s = stored(owner, MINI)
    assert validate(s, None)["ok"]
    s.refresh_from_db()
    assert s.status == "validated"
    rules_service.create_rule(approver, name="postcodeIsText", type="type_map",
                              params={"columnPattern": "^postcode$", "allowedTypes": ["text"]}, severity="block")
    assert not validate(s, None)["ok"]
    s.refresh_from_db()
    assert s.status == "validated"  # no downgrade


@pytest.mark.usefixtures("master")
def test_max_ops_per_script_blocks(owner, approver):
    from settings_app import tunables
    from config.siconfig import get_config
    tunables.apply(approver, {"limits.maxOpsPerScript": 1})
    r = validate(stored(owner, MINI), None)
    tunables.apply(approver, {"limits.maxOpsPerScript": None})  # reset for the next test
    get_config.cache_clear()
    assert not r["ok"]
    assert any("maxOpsPerScript" in e["message"] for e in r["errors"] + r["gate"]["errors"])


@pytest.mark.usefixtures("master")
def test_char_padded_parent_keys_still_match(owner):
    text = MINI.replace("code varchar(10) PRIMARY KEY", "code char(10) PRIMARY KEY")
    r = validate(stored(owner, text), None)
    assert r["ok"], r


@pytest.mark.usefixtures("master")
def test_unexpected_dry_run_exception_is_reported_not_crashed(owner, monkeypatch):
    import scripts.validate as v
    s = stored(owner, MINI)

    def boom(script, parsed=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(v, "make_row_source", boom)
    r = validate(s, None)
    assert not r["ok"] and r["dry_run"]["skipped"] is False
    assert r["dry_run"]["ok"] is False and "boom" in (r["dry_run"]["error"] or "")


def test_row_source_accepts_preparsed(owner):
    s = stored(owner, MINI)
    parsed = parse_script(MINI)
    rs = make_row_source(s, parsed=parsed)
    assert list(rs(bulk_op(s.id))) == [("พระนคร", "1001", "10"), ("เมืองสมุทรปราการ", "1101", "11")]
