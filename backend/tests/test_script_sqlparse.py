"""scripts.sqlparse: SQL script text -> raw gate ops + row streams (never executes SQL)."""
from pathlib import Path

from gate.operations import parse_ops
from scripts.sqlparse import ParsedScript, RowStream, ScriptIssue, iter_copy_rows, parse_script

MINI = (Path(__file__).parent / "data" / "mini_dump.sql").read_text(encoding="utf-8")


def ops_of(p: ParsedScript, kind: str) -> list[dict]:
    return [o for o in p.ops if o["op"] == kind]


def table(p: ParsedScript, name: str) -> dict:
    return next(o for o in ops_of(p, "create_table") if o["name"] == name)


def col(t: dict, name: str) -> dict:
    return next(c for c in t["columns"] if c["name"] == name)


# ---------- the real dump (trimmed) ----------

def test_mini_dump_parses_without_errors():
    p = parse_script(MINI)
    assert p.errors == []


def test_mini_dump_creates_three_tables_in_dependency_order():
    p = parse_script(MINI)
    names = [o["name"] for o in ops_of(p, "create_table")]
    assert names == ["province", "district", "subdistrict"]
    # every create_table comes before every foreign key
    kinds = [o["op"] for o in p.ops]
    assert max(i for i, k in enumerate(kinds) if k == "create_table") < kinds.index("add_foreign_key")


def test_mini_dump_columns_types_and_folded_primary_keys():
    p = parse_script(MINI)
    d = table(p, "district")
    assert [c["name"] for c in d["columns"]] == ["district_id", "district_name", "province_id"]
    assert col(d, "district_id") == {"name": "district_id", "type": "varchar(10)", "nullable": False,
                                     "primary_key": True}
    assert col(d, "district_name") == {"name": "district_name", "type": "varchar(100)"}
    assert col(table(p, "province"), "province_id")["primary_key"] is True
    assert col(table(p, "subdistrict"), "postcode")["type"] == "varchar(5)"


def test_mini_dump_foreign_keys_with_on_delete_restrict():
    p = parse_script(MINI)
    fks = ops_of(p, "add_foreign_key")
    assert {"op": "add_foreign_key", "table": "district", "column": "province_id", "ref_table": "province",
            "ref_column": "province_id", "name": "fk_district_province", "on_delete": "restrict"} in fks
    assert any(f["table"] == "subdistrict" and f["ref_table"] == "district" and f["on_delete"] == "restrict"
               for f in fks)


def test_mini_dump_check_and_indexes():
    p = parse_script(MINI)
    [chk] = ops_of(p, "add_check")
    assert chk["table"] == "subdistrict" and chk["name"] == "chk_subdistrict_postcode"
    assert "postcode" in chk["expression"] and "^[0-9]{5}$" in chk["expression"]
    idx = ops_of(p, "add_index")
    assert {"op": "add_index", "table": "district", "columns": ["province_id"],
            "name": "idx_district_province_id"} in idx


def test_mini_dump_ops_have_valid_gate_shape_and_check_expression_is_allowed():
    from gate.operations import check_expression
    p = parse_script(MINI)
    parse_ops(p.ops, max_ops=1000)
    [chk] = ops_of(p, "add_check")
    assert check_expression(chk["expression"], {"postcode"}) == []


def test_mini_dump_ignored_lines():
    p = parse_script(MINI)
    joined = "\n".join(p.ignored)
    assert "\\restrict" in joined and "\\unrestrict" in joined
    assert "SET statement_timeout" in joined
    assert "set_config" in joined
    assert "CREATE SCHEMA master_thach" in joined
    assert "OWNER TO" in joined
    assert p.ignored[0].startswith("line 5: ")


def test_mini_dump_streams_lazy_rows_with_thai_text():
    p = parse_script(MINI)
    assert set(p.streams) == {"district", "province", "subdistrict"}
    s = p.streams["subdistrict"]
    assert isinstance(s, RowStream)
    assert s.table == "subdistrict"
    assert s.columns == ["subdistrict_id", "subdistrict_name", "postcode", "district_id"]
    rows = list(s.rows())
    assert rows == [["100101", "พระบรมมหาราชวัง", "10200", "1001"],
                    ["100102", "วังบูรพาภิรมย์", "10200", "1001"],
                    ["110101", "ปากน้ำ", "10270", "1101"]]
    assert list(s.rows()) == rows  # can be read again
    assert list(p.streams["province"].rows()) == [["10", "กรุงเทพมหานคร"], ["11", "สมุทรปราการ"]]


def test_crlf_parses_same_as_lf():
    a = parse_script(MINI)
    b = parse_script(MINI.replace("\n", "\r\n"))
    assert b.errors == [] and b.ops == a.ops and b.ignored == a.ignored
    assert {t: list(s.rows()) for t, s in b.streams.items()} == {t: list(s.rows()) for t, s in a.streams.items()}


# ---------- errors ----------

def test_drop_table_is_an_error_with_its_line():
    p = parse_script("CREATE TABLE a (id integer);\n\n-- note\nDROP TABLE x;\n")
    assert len(p.errors) == 1
    e = p.errors[0]
    assert isinstance(e, ScriptIssue)
    assert e.line == 4 and e.statement.startswith("DROP TABLE x") and "not allowed" in e.message
    assert [o["op"] for o in p.ops] == ["create_table"]


def test_empty_and_comment_only_text():
    for text in ("", "   \n\n", "-- just a comment\n/* block\n comment */\n"):
        p = parse_script(text)
        assert [e.message for e in p.errors] == ["no statements"]
        assert p.ops == [] and p.streams == {}


def test_unknown_type_is_named():
    p = parse_script("CREATE TABLE a (id integer, shape geometry, tags text[]);")
    msgs = " ".join(e.message for e in p.errors)
    assert "geometry" in msgs and "text[]" in msgs
    assert p.ops == []


def test_type_mapping():
    p = parse_script(
        "CREATE TABLE a (a int4, b int8, c smallint, d bool, e character varying(20), f numeric(10,2),"
        " g timestamp with time zone, h timestamp without time zone, i double precision, j real, k uuid,"
        " l jsonb, m date, n text, o varchar, p serial, q bigserial);")
    assert p.errors == []
    t = table(p, "a")
    assert [c["type"] for c in t["columns"]] == [
        "integer", "bigint", "smallint", "boolean", "varchar(20)", "numeric(10,2)", "timestamptz", "timestamp",
        "double precision", "real", "uuid", "jsonb", "date", "text", "text", "integer", "bigint"]
    assert col(t, "p")["identity"] is True and col(t, "q")["identity"] is True


def test_defaults():
    p = parse_script(
        "CREATE TABLE a (a integer DEFAULT -3, b text DEFAULT 'x'::text, c boolean DEFAULT true,"
        " d timestamptz DEFAULT now(), e timestamptz DEFAULT CURRENT_TIMESTAMP, f date DEFAULT CURRENT_DATE,"
        " g uuid DEFAULT gen_random_uuid(), h numeric(5,2) DEFAULT 1.5);")
    assert p.errors == []
    t = table(p, "a")
    assert [c["default"] for c in t["columns"]] == [-3, "x", True, "now()", "now()", "current_date",
                                                     "gen_random_uuid()", 1.5]


def test_unsupported_default_is_an_error():
    p = parse_script("CREATE TABLE a (id integer DEFAULT nextval('a_id_seq'::regclass));")
    assert len(p.errors) == 1 and "default" in p.errors[0].message and "nextval" in p.errors[0].message


def test_disallowed_statements_each_reported():
    text = (
        "GRANT SELECT ON master_thach.a TO bob;\n"
        "CREATE EXTENSION postgis;\n"
        "CREATE SEQUENCE s;\n"
        "ALTER SEQUENCE s OWNED BY a.id;\n"
        "DO $$ BEGIN END $$;\n"
        "TRUNCATE a;\n"
    )
    p = parse_script(text)
    assert [e.line for e in p.errors] == [1, 2, 3, 4, 5, 6]


def test_dollar_quoted_text_with_semicolons():
    text = (
        "CREATE FUNCTION f() RETURNS integer AS $body$ SELECT 1; SELECT 2; $body$ LANGUAGE sql;\n"
        "CREATE TABLE a (id integer);\n"
        "COMMENT ON TABLE a IS $$one; two$$;\n"
        "COMMENT ON COLUMN a.id IS 'it''s; fine';\n"
    )
    p = parse_script(text)
    assert len(p.errors) == 1 and p.errors[0].line == 1 and "CREATE FUNCTION" in p.errors[0].statement
    assert {"op": "set_table_comment", "table": "a", "comment": "one; two"} in p.ops
    assert {"op": "set_column_comment", "table": "a", "column": "id", "comment": "it's; fine"} in p.ops


def test_copy_block_data_is_not_sql():
    text = (
        "CREATE TABLE a (id integer, note text);\n"
        "COPY a (id, note) FROM stdin;\n"
        "1\tCOMMIT;\n"
        "2\tDROP TABLE x; -- \\\\. not the end\n"
        "3\t\\N\n"
        "\\.\n"
        "CREATE INDEX a_note ON a (note);\n"
    )
    p = parse_script(text)
    assert p.errors == []
    assert list(p.streams["a"].rows()) == [["1", "COMMIT;"], ["2", "DROP TABLE x; -- \\. not the end"],
                                           ["3", None]]
    assert [o["op"] for o in p.ops] == ["create_table", "add_index"]
    assert not any("COMMIT" in s for s in p.ignored)


def test_unterminated_copy_block_is_an_error():
    p = parse_script("COPY a (id) FROM stdin;\n1\n2\n")
    assert any("not terminated" in e.message and e.line == 1 for e in p.errors)


def test_insert_values_stream():
    text = (
        "CREATE TABLE master_thach.a (id integer, name text, ok boolean);\n"
        "INSERT INTO master_thach.a (id, name, ok) VALUES (1, 'x;y', true), (2, NULL, false);\n"
        "INSERT INTO master_thach.a VALUES (3, 'z', NULL);\n"
        "INSERT INTO a (id) VALUES (now());\n"
    )
    p = parse_script(text)
    assert len(p.errors) == 1 and p.errors[0].line == 4 and "constant" in p.errors[0].message
    s = p.streams["a"]
    assert s.columns == ["id", "name", "ok"]
    assert list(s.rows()) == [["1", "x;y", "true"], ["2", None, "false"], ["3", "z", None]]


def test_unknown_schema_and_second_schema():
    text = (
        "CREATE TABLE master_thach.a (id integer);\n"
        "CREATE TABLE other.b (id integer);\n"
        "CREATE INDEX i ON other.b (id);\n"
    )
    p = parse_script(text)
    assert [e.line for e in p.errors] == [2, 3]
    assert all("schema" in e.message and "other" in e.message for e in p.errors)
    assert [o["name"] for o in ops_of(p, "create_table")] == ["a"]


def test_create_schema_other_than_source_is_an_error():
    p = parse_script("CREATE SCHEMA one;\nCREATE SCHEMA two;\nCREATE TABLE one.a (id integer);\n")
    assert [e.line for e in p.errors] == [2]


def test_meta_lines_ignored_but_include_is_error():
    p = parse_script("\\connect db\nCREATE TABLE a (id integer);\n\\i other.sql\n")
    assert [e.line for e in p.errors] == [3]
    assert any(s == "line 1: \\connect db" for s in p.ignored)


def test_alter_table_constraints_folded_and_ops():
    text = (
        "CREATE TABLE p (id integer NOT NULL, code text);\n"
        "CREATE TABLE c (id integer, p_id integer, a text, b text,"
        " CONSTRAINT c_p FOREIGN KEY (p_id) REFERENCES p ON DELETE CASCADE);\n"
        "ALTER TABLE ONLY p ADD CONSTRAINT p_pkey PRIMARY KEY (id);\n"
        "ALTER TABLE ONLY p ADD CONSTRAINT p_code_key UNIQUE (code);\n"
        "ALTER TABLE c ADD CONSTRAINT c_ab UNIQUE (a, b);\n"
        "ALTER TABLE c ADD CONSTRAINT c_a_chk CHECK (length(a) > 1);\n"
        "ALTER TABLE c ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (SEQUENCE NAME c_id_seq);\n"
        "ALTER TABLE c ADD CONSTRAINT c_pkey PRIMARY KEY (id);\n"
        "SELECT pg_catalog.setval('c_id_seq', 5, true);\n"
    )
    p = parse_script(text)
    assert p.errors == []
    assert col(table(p, "p"), "id")["primary_key"] is True
    assert col(table(p, "c"), "id")["identity"] is True
    assert {"op": "add_unique", "table": "p", "columns": ["code"], "name": "p_code_key"} in p.ops
    assert {"op": "add_unique", "table": "c", "columns": ["a", "b"], "name": "c_ab"} in p.ops
    assert {"op": "add_foreign_key", "table": "c", "column": "p_id", "ref_table": "p", "ref_column": None,
            "name": "c_p", "on_delete": "cascade"} in p.ops
    assert any(o["op"] == "add_check" and o["name"] == "c_a_chk" for o in p.ops)
    parse_ops(p.ops)


def test_unsupported_constraint_forms():
    text = (
        "CREATE TABLE a (x integer, y integer, PRIMARY KEY (x, y));\n"
        "CREATE TABLE b (x integer);\n"
        "ALTER TABLE b ADD CONSTRAINT b_fk FOREIGN KEY (x) REFERENCES a (x) ON DELETE SET DEFAULT;\n"
        "ALTER TABLE b ADD COLUMN z text;\n"
    )
    p = parse_script(text)
    assert [e.line for e in p.errors] == [1, 3, 4]


def test_referenced_table_created_first_even_if_declared_later():
    text = (
        "CREATE TABLE child (id integer, parent_id integer REFERENCES parent (id));\n"
        "CREATE TABLE parent (id integer PRIMARY KEY);\n"
    )
    p = parse_script(text)
    assert p.errors == []
    assert [o["name"] for o in ops_of(p, "create_table")] == ["parent", "child"]
    # faithful to the script: PostgreSQL's default for a missing ON DELETE is NO ACTION
    assert ops_of(p, "add_foreign_key") == [{"op": "add_foreign_key", "table": "child", "column": "parent_id",
                                             "ref_table": "parent", "ref_column": "id", "name": None,
                                             "on_delete": "no action"}]


def test_iter_copy_rows_unescapes_and_stops_at_terminator():
    lines = ["a\\tb\tline\\nbreak\t\\\\x\t\\N", "\\r\t", "\\.", "never"]
    assert list(iter_copy_rows(lines)) == [["a\tb", "line\nbreak", "\\x", None], ["\r", ""]]


# ---------- fix round 1 ----------

def test_text_after_copy_on_same_line_is_an_error():
    p = parse_script("CREATE TABLE a (id integer);\nCOPY a (id) FROM stdin; DROP TABLE zz;\n1\n\\.\n")
    assert [(e.line, e.statement) for e in p.errors] == [(2, "DROP TABLE zz;")]
    assert "same line" in p.errors[0].message
    ok = parse_script("CREATE TABLE a (id integer);\nCOPY a (id) FROM stdin; -- fine /* x */\n1\n\\.\n")
    assert ok.errors == [] and list(ok.streams["a"].rows()) == [["1"]]
    ok2 = parse_script("CREATE TABLE a (id integer);\nCOPY a (id) FROM stdin; /* c */  \n1\n\\.\n")
    assert ok2.errors == []


def test_schema_checked_on_ignored_statements():
    text = (
        "CREATE TABLE s.a (id integer);\n"
        "ALTER TABLE other.a OWNER TO x;\n"
        "ALTER SCHEMA other OWNER TO x;\n"
        "ALTER FUNCTION other.f() OWNER TO x;\n"
        "COMMENT ON SCHEMA other IS 'x';\n"
        "ALTER TABLE s.a OWNER TO x;\n"
        "ALTER SCHEMA s OWNER TO x;\n"
        "COMMENT ON SCHEMA s IS 'mine';\n"
    )
    p = parse_script(text)
    assert [e.line for e in p.errors] == [2, 3, 4, 5]
    assert all("other" in e.message for e in p.errors)
    assert [i.split(":")[0] for i in p.ignored] == ["line 6", "line 7", "line 8"]


def test_comment_on_schema_fixes_the_source_schema():
    p = parse_script("COMMENT ON SCHEMA other IS 'x';\nCREATE TABLE s.a (id integer);\n")
    assert [e.line for e in p.errors] == [2]


def test_set_search_path_rules():
    p = parse_script(
        "CREATE TABLE s.a (id integer);\n"
        "SET search_path = s;\n"
        "SET search_path = '';\n"
        "SET search_path = evil, public;\n"
        "SET search_path TO DEFAULT;\n"
    )
    assert [e.line for e in p.errors] == [4, 5]


def test_string_semantics_settings_are_errors():
    p = parse_script(
        "SET standard_conforming_strings = on;\n"
        "SET standard_conforming_strings = off;\n"
        "SET backslash_quote = on;\n"
        "SET client_encoding = 'UTF8';\n"
        "SET client_encoding = 'LATIN1';\n"
        "SET escape_string_warning = off;\n"
        "CREATE TABLE a (id integer);\n"
    )
    assert [e.line for e in p.errors] == [2, 3, 5]
    assert "standard_conforming_strings" in p.errors[0].message


def test_set_config_only_search_path():
    p = parse_script(
        "SELECT pg_catalog.set_config('search_path', '', false);\n"
        "SELECT pg_catalog.set_config('role', 'postgres', false);\n"
        "CREATE TABLE s.a (id integer);\n"
        "SELECT set_config('search_path', 'evil', false);\n"
        "SELECT set_config('search_path', 's', false);\n"
    )
    assert [e.line for e in p.errors] == [2, 4]


def test_setval_forms_ignored_others_error():
    p = parse_script(
        "CREATE TABLE s.t (c integer);\n"
        "SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('s.t', 'c'), 77, true);\n"
        "SELECT pg_catalog.setval('s.t_c_seq', 5, false);\n"
        "SELECT setval('t_c_seq', 5);\n"
        "SELECT pg_catalog.setval('x', pg_catalog.nextval('y'));\n"
        "SELECT setval(pg_get_serial_sequence('other.t', 'c'), 1);\n"
        "SELECT 1;\n"
    )
    assert [e.line for e in p.errors] == [5, 6, 7]
    assert len(p.ignored) == 3


def test_numbers_keep_their_literal_text():
    p = parse_script(
        "CREATE TABLE a (id integer, f numeric);\n"
        "INSERT INTO a VALUES (1, 123456789012345678901234.55), (2, 1.10), (3, 1e400), "
        "(123456789012345678901234567890, -0.5);\n"
    )
    assert p.errors == []
    assert list(p.streams["a"].rows()) == [["1", "123456789012345678901234.55"], ["2", "1.10"],
                                           ["3", "1e400"], ["123456789012345678901234567890", "-0.5"]]


def test_numeric_defaults_are_exact():
    p = parse_script(
        "CREATE TABLE a (a numeric(5,2) DEFAULT 1.5, b numeric DEFAULT 123456789012345678901234.55,"
        " c numeric DEFAULT 1e400, d numeric DEFAULT 0.1, e bigint DEFAULT 123456789012345678901234567890);")
    assert p.errors == []
    assert [c["default"] for c in table(p, "a")["columns"]] == [
        1.5, "123456789012345678901234.55", "1e400", 0.1, 123456789012345678901234567890]


def test_unique_index_comes_before_foreign_keys():
    text = (
        "CREATE TABLE p (id integer, code text);\n"
        "CREATE TABLE c (id integer, p_code text);\n"
        "ALTER TABLE c ADD CONSTRAINT c_p FOREIGN KEY (p_code) REFERENCES p (code);\n"
        "CREATE UNIQUE INDEX p_code_idx ON p (code);\n"
        "CREATE INDEX c_p_code_idx ON c (p_code);\n"
    )
    p = parse_script(text)
    assert p.errors == []
    kinds = [(o["op"], o.get("name")) for o in p.ops]
    assert kinds.index(("add_index", "p_code_idx")) < kinds.index(("add_foreign_key", "c_p"))
    assert {"op": "add_index", "table": "p", "columns": ["code"], "name": "p_code_idx", "unique": True} in p.ops


def test_rejected_statement_does_not_fix_the_schema():
    p = parse_script(
        "CREATE TABLE evil.z (x geometry);\n"
        "DROP TABLE evil.x;\n"
        "CREATE TABLE good.a (id integer);\n"
    )
    assert [e.line for e in p.errors] == [1, 2]
    assert [o["name"] for o in ops_of(p, "create_table")] == ["a"]


def test_identifiers_must_match_gate_rule():
    p = parse_script(
        'CREATE TABLE "a b" (id integer);\n'
        'CREATE TABLE ok ("x y" integer);\n'
        'CREATE TABLE ok2 (id integer, CONSTRAINT "c;x" CHECK (id > 0));\n'
        'CREATE TABLE ok3 (id integer);\n'
        'CREATE INDEX "i;x" ON ok3 (id);\n'
        'COPY ok3 ("z z") FROM stdin;\n\\.\n'
        'CREATE TABLE ' + "a" * 64 + ' (id integer);\n'
        'CREATE TABLE ok4 ("' + "b" * 70 + '" integer);\n'
        'CREATE TABLE ' + "c" * 63 + ' (id integer);\n'
    )
    assert [e.line for e in p.errors] == [1, 2, 3, 5, 6, 8, 9]
    assert "longer than 63" in p.errors[-1].message and "b" * 70 in p.errors[-1].message
    assert [o["name"] for o in ops_of(p, "create_table")] == ["ok3", "c" * 63]


def test_copy_row_width_checked_lazily_with_line():
    from scripts.sqlparse import RowStreamError
    import pytest
    p = parse_script("CREATE TABLE a (id integer, t text);\nCOPY a (id, t) FROM stdin;\n1\tx\n1\t2\t3\n\\.\n")
    assert p.errors == []
    it = p.streams["a"].rows()
    assert next(it) == ["1", "x"]
    with pytest.raises(RowStreamError) as exc:
        next(it)
    assert exc.value.issue.line == 4 and "3 fields, expected 2" in exc.value.issue.message


def test_insert_row_width_is_an_error():
    p = parse_script("CREATE TABLE a (id integer, t text);\nINSERT INTO a VALUES (1);\n")
    assert [e.line for e in p.errors] == [2]


def test_create_index_forms_rejected():
    p = parse_script(
        "CREATE TABLE a (id integer);\n"
        "CREATE INDEX CONCURRENTLY i1 ON a (id);\n"
        "CREATE INDEX IF NOT EXISTS i2 ON a (id);\n"
        "CREATE INDEX ON a (id);\n"
        "CREATE INDEX i4 ON ONLY a (id);\n"
    )
    msgs = [e.message for e in p.errors]
    assert [e.line for e in p.errors] == [2, 3, 4, 5]
    assert "CONCURRENTLY" in msgs[0] and "IF NOT EXISTS" in msgs[1] and "name" in msgs[2] and "ONLY" in msgs[3]


def test_clear_messages_for_create_table_as_and_mixed_owner():
    p = parse_script(
        "CREATE TABLE a AS SELECT 1 AS x;\n"
        "CREATE TABLE b (id integer);\n"
        "ALTER TABLE b OWNER TO bob, ADD CONSTRAINT b_pk PRIMARY KEY (id);\n"
    )
    assert "CREATE TABLE AS" in p.errors[0].message
    assert "OWNER TO cannot be combined" in p.errors[1].message


def test_check_expression_validated_early():
    p = parse_script("CREATE TABLE a (id integer CHECK (id IN (SELECT 1)));\n"
                     "CREATE TABLE b (id integer);\nALTER TABLE b ADD CONSTRAINT k CHECK (pg_sleep(1) IS NULL);\n")
    assert [e.line for e in p.errors] == [1, 3]


def test_lexing_edge_cases():
    text = (
        "COMMENT ON TABLE a IS E'x\\';DROP TABLE y;--';\n"
        "/* a /* b */ ; DROP TABLE x; */ CREATE TABLE a (id integer);\n"
        "CREATE TABLE b (id integer); CREATE TABLE c (id integer); DROP TABLE q;\n"
        "CREATE TABLE d (t text);\n"
        "COPY d (t) FROM stdin;\nfoo\\.bar\n\\.x\n\\.\n"
    )
    p = parse_script(text)
    assert [(e.line, e.statement) for e in p.errors] == [(3, "DROP TABLE q;")]
    assert {"op": "set_table_comment", "table": "a", "comment": "x';DROP TABLE y;--"} in p.ops
    assert [o["name"] for o in ops_of(p, "create_table")] == ["a", "b", "c", "d"]
    assert list(p.streams["d"].rows()) == [["foo.bar"], [".x"]]


def test_lone_semicolons_are_no_statements():
    assert [e.message for e in parse_script(";;;\n").errors] == ["no statements"]


# ---------- fix round 2 ----------

def test_copy_trailing_text_between_comments_is_an_error():
    T = "CREATE TABLE a (id integer);\n"
    bad = [
        "COPY a (id) FROM stdin; /* x */ 5 /* y */\n1\n\\.\n",
        "COPY a (id) FROM stdin; /* x */ DROP TABLE b; -- */\n1\n\\.\n",
        "COPY a (id) FROM stdin; /* x */ DROP TABLE b; /* y */\n1\n\\.\n",
        "COPY a (id) FROM stdin; /* unclosed\n1\n\\.\n",
        "COPY a (id) FROM stdin; /* a /* b */ c\n1\n\\.\n",
    ]
    for tail in bad:
        p = parse_script(T + tail)
        assert any("same line" in e.message and e.line == 2 for e in p.errors), tail
    good = [
        "COPY a (id) FROM stdin; -- fine\n1\n\\.\n",
        "COPY a (id) FROM stdin; /* ok */\n1\n\\.\n",
        "COPY a (id) FROM stdin; /* a /* b */ c */ -- x\n1\n\\.\n",
    ]
    for tail in good:
        p = parse_script(T + tail)
        assert p.errors == [] and list(p.streams["a"].rows()) == [["1"]], tail


def test_long_identifier_after_multibyte_text():
    def make(n):
        return f"CREATE TABLE t (a text DEFAULT 'ไทยไทย', \"{'q' * n}\" integer);"
    assert parse_script(make(62)).errors == []
    assert parse_script(make(63)).errors == []
    p = parse_script(make(64))
    assert len(p.errors) == 1 and "longer than 63" in p.errors[0].message


def test_identifier_with_trailing_newline_rejected():
    p = parse_script('CREATE TABLE "x\n" (id integer);\nCREATE TABLE y ("c\n" integer);\n')
    assert [e.line for e in p.errors] == [1, 3]


def test_search_path_allows_pg_catalog_and_user():
    p = parse_script(
        "CREATE TABLE s.a (id integer);\n"
        "SET search_path = s, pg_catalog;\n"
        "SET search_path = \"$user\", s;\n"
        "SELECT pg_catalog.set_config('search_path', 's, pg_catalog', false);\n"
        "SELECT set_config('search_path', '\"$user\", pg_catalog', false);\n"
        "SET search_path = s, other;\n"
        "SELECT set_config('search_path', NULL, false);\n"
    )
    assert [e.line for e in p.errors] == [6, 7]
    assert "other" in p.errors[0].message and "text value" in p.errors[1].message
