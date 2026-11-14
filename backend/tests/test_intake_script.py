import pytest

from intake.profile import profile
from intake.script import apply_filter, write
from scripts import copyparse, sqlparse


def src(name, headers, rows):
    return {"name": name, "headers": headers, "rows": rows, "notes": [], "truncated": False}


def build(sources, plan=None, existing=()):
    profiles = [profile(s, 5) for s in sources]
    plan = dict(plan or {})
    plan.setdefault("existing", list(existing))
    return write(sources, profiles, plan, "master").decode("utf-8")


def rows_of(text, table):
    parsed = sqlparse.parse_script(text)
    assert not parsed.errors, parsed.errors
    return parsed, list(parsed.streams[table].rows())


VENDORS = src("vendors", ["Name", "Qty", "Price", "Since", "Active"],
              [["A", "1", "1.5", "2024-01-31", "yes"], ["B", "2", "2", "2023-12-01", "no"]])


def test_new_table_has_typed_columns_and_copy():
    text = build([VENDORS])
    parsed, rows = rows_of(text, "vendors")
    create = next(o for o in parsed.ops if o["op"] == "create_table")
    types = {c["name"]: c["type"] for c in create["columns"]}
    assert types == {"Name": "text", "Qty": "bigint", "Price": "numeric", "Since": "date", "Active": "boolean"}
    assert rows == [["A", "1", "1.5", "2024-01-31", "yes"], ["B", "2", "2", "2023-12-01", "no"]]


def test_existing_target_has_only_copy():
    text = build([VENDORS], {"mapping": {"tables": {"vendors": {"target": "partner", "columns": {}}}}},
                 existing=["partner"])
    assert "CREATE TABLE" not in text
    assert text.count("COPY ") == 1
    assert 'COPY "vendors" ("Name", "Qty", "Price", "Since", "Active") FROM stdin;' in text


def test_target_missing_from_existing_creates():
    text = build([VENDORS], {"mapping": {"tables": {"vendors": {"target": "partner"}}}}, existing=["other"])
    assert 'CREATE TABLE "vendors"' in text


def test_filter_and_skip_remove_rows():
    s = src("t", ["k", "status"], [["1", "on"], ["2", "off"], ["3", "on"], ["4", "on"]])
    text = build([s], {"filters": {"t": [{"column": "status", "op": "equals", "value": "on"}]},
                       "skip": {"t": [3]}})
    _, rows = rows_of(text, "t")
    assert rows == [["1", "on"], ["4", "on"]]


def test_thai_tab_newline_backslash_null_roundtrip():
    vals = ["ไทย\tแท็บ", "line1\nline2", "back\\slash", "cr\rx", "\\N", None]
    s = src("t", ["v"], [[v] for v in vals])
    text = build([s])
    _, rows = rows_of(text, "t")
    assert rows == [[v] for v in ["ไทย\tแท็บ", "line1\nline2", "back\\slash", "cr\rx", "\\N", None]]
    block = text.split("FROM stdin;\n", 1)[1]
    assert list(copyparse.iter_copy(block)) == rows


def test_identifiers_are_quoted_and_doubled():
    s = src('we"ird', ['a"b', "c d"], [["1", "x"]])
    text = build([s])
    assert 'CREATE TABLE "we""ird" (\n    "a""b" bigint,\n    "c d" text\n);' in text
    assert 'COPY "we""ird" ("a""b", "c d") FROM stdin;\n1\tx\n\\.\n' in text
    # the parser (not the writer) refuses such names
    assert sqlparse.parse_script(text).errors


def test_hyphenated_names_parse():
    _, rows = rows_of(build([src("my-sheet", ["col-1"], [["1"]])]), "my-sheet")
    assert rows == [["1"]]


def test_thai_conversion_applied():
    s = src("t", ["d", "n"], [["๒๕๖๗-๐๑-๓๑", "๑๒"], ["๒๕๖๖-๑๒-๐๑", "๓"]])
    text = build([s])
    _, rows = rows_of(text, "t")
    assert rows == [["2024-01-31", "12"], ["2023-12-01", "3"]]
    assert '"d" date' in text and '"n" bigint' in text


def test_multiple_sources_and_short_rows():
    a = src("a", ["x", "y"], [["1"]])
    b = src("b", ["z"], [["q"]])
    text = build([a, b])
    parsed, _ = rows_of(text, "a")
    assert set(parsed.streams) == {"a", "b"}
    assert list(parsed.streams["a"].rows()) == [["1", None]]


def test_apply_filter_ops():
    headers = ["a", "b"]
    rows = [["x", "1"], ["y", ""], ["x", None], ["z", " 2 "]]
    assert apply_filter(rows, headers, []) == [0, 1, 2, 3]
    assert apply_filter(rows, headers, [{"column": "a", "op": "equals", "value": "x"}]) == [0, 2]
    assert apply_filter(rows, headers, [{"column": "a", "op": "in", "value": ["y", "z"]}]) == [1, 3]
    assert apply_filter(rows, headers, [{"column": "b", "op": "not_empty"}]) == [0, 3]
    assert apply_filter(rows, headers, [{"column": "a", "op": "equals", "value": "x"},
                                        {"column": "b", "op": "not_empty"}]) == [0]
    with pytest.raises(ValueError):
        apply_filter(rows, headers, [{"column": "nope", "op": "equals", "value": "x"}])
    with pytest.raises(ValueError):
        apply_filter(rows, headers, [{"column": "a", "op": "like", "value": "x"}])


def test_existing_detected_from_schema_when_plan_silent(master):
    from schema import introspect
    assert "t" not in introspect.snapshot("master")
    s = src("t", ["k"], [["1"]])
    text = write([s], [profile(s, 5)], {}, "master").decode()
    assert "CREATE TABLE" in text
