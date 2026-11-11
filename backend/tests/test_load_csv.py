import pytest

from scripts.copyparse import unescape_field
from scripts.loadcsv import CsvError, check_cells, parse_csv, to_copy_line


def test_null_vs_empty():
    assert parse_csv('a,,"",b\n') == [["a", None, "", "b"]]


def test_quotes_and_newlines():
    assert parse_csv('"x,""y""\nz",2\n') == [['x,"y"\nz', "2"]]


def test_crlf_and_no_trailing_newline():
    assert parse_csv("a,b\r\nc,d") == [["a", "b"], ["c", "d"]]


@pytest.mark.parametrize("text,row", [
    ('a,"b\n', 1), ('x,y\nab"c,1\n', 2), ('"a"b,1\n', 1), ("a,b\n\nc,d\n", 2)])
def test_csv_errors(text, row):
    with pytest.raises(CsvError) as e:
        parse_csv(text)
    assert e.value.row == row


def test_copy_line_escapes():
    row = ["a\tb", "c\\d", "e\nf", None, "\\N", "é"]
    assert to_copy_line(row) == "a\\tb\tc\\\\d\te\\nf\t\\N\t\\\\N\té"


def test_copy_roundtrip():
    row = ["a\tb", "c\\d", "e\nf", "\\N", "é", "x\ry"]
    fields = to_copy_line(row).split("\t")
    assert [unescape_field(f) for f in fields] == row


def col(name, type_, req=False):
    return {"name": name, "type": type_, "nullable": not req, "required_value": req}


GOOD_BAD = [
    ("integer", "12", "1.5"), ("bigint", "-7", "1e3"), ("smallint", "+3", "x"),
    ("numeric", "1.25", "abc"), ("double precision", "NaN", "abc"), ("real", "2.5", "1,5"),
    ("boolean", "True", "maybe"), ("uuid", "123e4567-e89b-12d3-a456-426614174000", "123"),
    ("date", "2026-11-11", "11/11/2026"), ("date", "2026-02-28", "2026-02-30"),
    ("timestamp without time zone", "2026-11-11 10:30:00", "yesterday"),
    ("timestamp with time zone", "2026-11-11T10:30:00+07:00", "2026-11-11 25:00"),
    ("varchar(3)", "abc", "abcd"), ("character varying(3)", "abc", "abcd"),
    ("mood", "happy", "angry"),
]


@pytest.mark.parametrize("type_,good,bad", GOOD_BAD)
def test_cells(type_, good, bad):
    cols = [col("a", type_)]
    enums = {"mood": ["happy", "sad"]}
    assert check_cells([[good]], cols, enums, 1) == []
    (p,) = check_cells([[bad]], cols, enums, 10)
    assert p["row"] == 10 and p["column"] == "a" and p["value"] == bad and p["message"]


def test_integer_range():
    assert check_cells([["32768"]], [col("a", "smallint")], {}, 1)
    assert check_cells([["-32768"]], [col("a", "smallint")], {}, 1) == []


def test_nan_refused_for_numeric():
    assert check_cells([["NaN"]], [col("a", "integer")], {}, 1)


def test_first_row_offsets_and_field_count_and_not_null():
    cols = [col("a", "text", True), col("b", "integer")]
    probs = check_cells([["x", "1"], ["x"], [None, "2"]], cols, {}, 100)  # rows 100,101,102
    assert [(p["row"], p["column"]) for p in probs] == [(101, None), (102, "a")]


def test_empty_string_on_non_text_is_null():
    assert check_cells([[""]], [col("a", "integer")], {}, 1) == []
    assert check_cells([[""]], [col("a", "integer", True)], {}, 1)
    assert check_cells([[""]], [col("a", "text", True)], {}, 1) == []


def test_unknown_type_passes():
    assert check_cells([["whatever"]], [col("a", "jsonb")], {}, 1) == []


def test_problem_cap():
    assert len(check_cells([["x"]] * 50, [col("a", "integer")], {}, 1)) == 20
