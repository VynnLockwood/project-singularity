"""SQL isolation, the standing guard: a differential corpus.

Each shape below names a database through `{S}` in some position a statement can hold a name: relations, types,
functions, operators, sequences, operator classes, collations, text search objects, `reg*` strings, plpgsql
bodies, DDL options, GRANT/COMMENT/ALTER/DROP/SET and so on. It runs as a master-only editor through
`propose_sql` (and, when it is a query, as a master-only viewer through `query`) with S = `crm`, a database
that exists and is full of matching objects, and with S = `nope`, which does not exist. The outcomes (status
or error text, with the typed name itself normalised) must be identical: a hidden database answers exactly
like a missing one. A shape the server would answer differently is refused by the scanner before it runs.
Add a shape whenever a new name position is found.
"""
import pytest

from accounts.models import Role, SchemaGrant, User
from approvals import service
from schema import conn as dbconn
from schema import query

from . import pgsetup

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master"), pytest.mark.no_default_access]

REASON = "corpus shape for the isolation guard"

SHAPES = [
    # ---- relations, in every clause
    "SELECT * FROM {S}.t",
    "SELECT * FROM master.t JOIN {S}.t USING (code)",
    "SELECT * FROM master.t m LEFT JOIN LATERAL (SELECT * FROM {S}.t) x ON true",
    "SELECT (SELECT count(*) FROM {S}.t)",
    "SELECT * FROM master.t WHERE code IN (SELECT code FROM {S}.t)",
    "SELECT * FROM master.t WHERE EXISTS (SELECT 1 FROM {S}.t)",
    "WITH x AS (SELECT * FROM {S}.t) SELECT * FROM x",
    "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM x, {S}.t WHERE n < 2) SELECT * FROM x",
    "SELECT * FROM master.t UNION SELECT * FROM {S}.t",
    "SELECT * FROM master.t EXCEPT SELECT * FROM {S}.t",
    "SELECT {S}.t.code FROM {S}.t",
    "SELECT {S}.t.* FROM {S}.t",
    "SELECT * FROM {S}.t TABLESAMPLE bernoulli (50)",
    "SELECT * FROM master.t TABLESAMPLE {S}.bernoulli (50)",
    "SELECT * FROM ONLY {S}.t",
    "SELECT * FROM master.t, {S}.t t2 FOR UPDATE OF t2",
    "INSERT INTO master.t SELECT * FROM {S}.t",
    "INSERT INTO {S}.t VALUES ('a', 'b')",
    "UPDATE master.t SET name = (SELECT name FROM {S}.t LIMIT 1)",
    "UPDATE master.t SET name = x.name FROM {S}.t x WHERE x.code = t.code",
    "DELETE FROM master.t USING {S}.t x WHERE x.code = t.code",
    "DELETE FROM {S}.t",
    "MERGE INTO master.t m USING {S}.t s ON s.code = m.code WHEN NOT MATCHED THEN INSERT VALUES (s.code, s.name)",
    "TRUNCATE {S}.t",
    "LOCK TABLE {S}.t IN SHARE MODE",
    "INSERT INTO master.t VALUES ('a', 'b') ON CONFLICT DO NOTHING RETURNING (SELECT count(*) FROM {S}.t)",
    "CREATE TABLE master.c1 AS SELECT * FROM {S}.t",
    "CREATE VIEW master.v1 AS SELECT * FROM {S}.t",
    "CREATE MATERIALIZED VIEW master.mv1 AS SELECT * FROM {S}.t",
    "REFRESH MATERIALIZED VIEW {S}.mv",
    "CREATE TABLE master.c2 (LIKE {S}.t)",
    "CREATE TABLE master.c3 () INHERITS ({S}.t)",
    "CREATE TABLE master.c4 (code text REFERENCES {S}.t (code))",
    "ALTER TABLE master.t ADD CONSTRAINT fk FOREIGN KEY (code) REFERENCES {S}.t (code)",
    "CREATE TABLE master.c5 PARTITION OF {S}.t FOR VALUES IN ('a')",
    "ALTER TABLE {S}.t ADD COLUMN x int",
    "ALTER TABLE master.t RENAME TO {S}",
    "ALTER TABLE master.t SET SCHEMA {S}",
    "ALTER TABLE {S}.t OWNER TO CURRENT_USER",
    "CREATE INDEX ON {S}.t (code)",
    "CLUSTER {S}.t USING t_pkey",
    "VACUUM ANALYZE {S}.t",
    "ANALYZE {S}.t (code)",
    "REINDEX TABLE {S}.t",
    "REINDEX SCHEMA {S}",
    "EXPLAIN SELECT * FROM {S}.t",
    "DROP TABLE {S}.t",
    "DROP VIEW {S}.v",
    "DROP INDEX {S}.t_pkey",
    "CREATE POLICY p ON master.t USING (code IN (SELECT code FROM {S}.t))",
    "CREATE POLICY p ON {S}.t USING (true)",
    "CREATE RULE r AS ON INSERT TO master.t DO ALSO INSERT INTO {S}.t VALUES (NEW.code, NEW.name)",
    # ---- types, domains, enums, casts
    "SELECT NULL::{S}.ty",
    "SELECT CAST(NULL AS {S}.e)",
    "SELECT 'hidden_a'::{S}.e",
    "SELECT enum_range(NULL::{S}.e)",
    "SELECT ARRAY[]::{S}.e[]",
    "SELECT NULL::{S}.dom",
    "CREATE TABLE master.c6 (a {S}.e)",
    "CREATE TABLE master.c7 (a {S}.dom[])",
    "ALTER TABLE master.t ALTER COLUMN name TYPE {S}.e USING name::{S}.e",
    "ALTER TABLE master.t ADD COLUMN x {S}.ty",
    "CREATE DOMAIN master.d1 AS {S}.dom",
    "CREATE TYPE master.ct AS (a {S}.e)",
    "ALTER TYPE {S}.e ADD VALUE 'x'",
    "ALTER TYPE {S}.e RENAME VALUE 'hidden_a' TO 'z'",
    "ALTER DOMAIN {S}.dom SET NOT NULL",
    "DROP TYPE {S}.e",
    "DROP DOMAIN {S}.dom",
    "CREATE FUNCTION master.cf1(a {S}.e) RETURNS int LANGUAGE sql AS 'select 1'",
    "CREATE FUNCTION master.cf2() RETURNS {S}.e LANGUAGE sql AS 'select null'",
    "CREATE FUNCTION master.cf3() RETURNS SETOF {S}.t LANGUAGE sql AS 'select null::{S}.t'",
    "SELECT json_populate_record(NULL::{S}.t, '{{}}')",
    "SELECT * FROM json_populate_recordset(NULL::{S}.t, '[]')",
    "CREATE CAST ({S}.ty AS text) WITH INOUT",
    # ---- functions and procedures
    "SELECT {S}.f(1)",
    "SELECT * FROM {S}.f(1)",
    "CALL {S}.p()",
    "SELECT master.t.code FROM master.t WHERE {S}.f(1) > 0",
    "CREATE FUNCTION master.cf4() RETURNS int LANGUAGE sql AS 'select {S}.f(1)'",
    "CREATE FUNCTION master.cf5() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT {S}.f(1); END",
    "CREATE TABLE master.c8 (a int DEFAULT {S}.f(1))",
    "ALTER TABLE master.t ALTER COLUMN code SET DEFAULT {S}.f(1)::text",
    "CREATE TABLE master.c9 (a int CHECK ({S}.f(a) > 0))",
    "CREATE TABLE master.c10 (a int, b int GENERATED ALWAYS AS ({S}.f(a)) STORED)",
    "CREATE INDEX ON master.t ({S}.f(length(code)))",
    "CREATE TRIGGER tg BEFORE INSERT ON master.t FOR EACH ROW EXECUTE FUNCTION {S}.tf()",
    "DROP FUNCTION {S}.f(int)",
    "ALTER FUNCTION {S}.f(int) RENAME TO g",
    "ALTER FUNCTION master.nofunc({S}.e) STABLE",
    "COMMENT ON FUNCTION {S}.f(int) IS 'x'",
    "GRANT EXECUTE ON FUNCTION {S}.f(int) TO PUBLIC",
    "REVOKE EXECUTE ON FUNCTION {S}.f(int) FROM PUBLIC",
    "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {S} TO PUBLIC",
    "SELECT count(*) FILTER (WHERE {S}.f(1) > 0) FROM master.t",
    "SELECT row_number() OVER (ORDER BY {S}.f(1)) FROM master.t",
    # ---- operators
    "SELECT 1 OPERATOR({S}.===) 1",
    "SELECT 1 WHERE 1 OPERATOR({S}.===) ANY (SELECT 1)",
    "SELECT code FROM master.t ORDER BY code USING OPERATOR({S}.<)",
    "CREATE TABLE master.x2 (c int, EXCLUDE USING btree (c WITH {S}.=))",
    "CREATE TABLE master.x3 (c int, EXCLUDE USING btree (c WITH OPERATOR({S}.=)))",
    "ALTER TABLE master.t ADD EXCLUDE USING btree (code WITH {S}.=)",
    "DROP OPERATOR {S}.=== (int, int)",
    "COMMENT ON OPERATOR {S}.=== (int, int) IS 'x'",
    "CREATE OPERATOR master.<<< (LEFTARG = int, RIGHTARG = int, FUNCTION = {S}.f2)",
    # ---- sequences and identity
    "SELECT nextval('{S}.s')",
    "SELECT currval('{S}.s')",
    "SELECT setval('{S}.s', 1)",
    "CREATE SEQUENCE master.s5 OWNED BY {S}.t.code",
    "ALTER SEQUENCE master.s0 OWNED BY {S}.t.code",
    "CREATE TABLE master.x1 (id int GENERATED ALWAYS AS IDENTITY (SEQUENCE NAME {S}.newseq))",
    "ALTER TABLE master.t ADD COLUMN id2 int GENERATED BY DEFAULT AS IDENTITY (SEQUENCE NAME {S}.newseq2)",
    "CREATE TABLE master.c11 (a int DEFAULT nextval('{S}.s'))",
    "ALTER SEQUENCE {S}.s RESTART",
    "DROP SEQUENCE {S}.s",
    "GRANT USAGE ON SEQUENCE {S}.s TO PUBLIC",
    "GRANT USAGE ON ALL SEQUENCES IN SCHEMA {S} TO PUBLIC",
    "SELECT pg_sequence_last_value('{S}.s')",
    # ---- reg* strings, in every shape
    "SELECT '{S}.t'::regclass",
    "SELECT '{S}.t'::pg_catalog.regclass",
    "SELECT CAST('{S}.t' AS regclass)",
    "SELECT '{{{S}.t,master.t}}'::regclass[]",
    "SELECT '{{{S}.t,master_test.master.t}}'::regclass[]",
    "SELECT '{S}.ty'::regtype",
    "SELECT '{{{S}.ty}}'::regtype[]",
    "SELECT '{S}.f'::regproc",
    "SELECT '{S}.f(int)'::regprocedure",
    "SELECT 'master.f({S}.t)'::regprocedure",
    "SELECT 'master.=({S}.t,{S}.t)'::regoperator",
    "SELECT '{S}.==='::regoper",
    "SELECT '{S}'::regnamespace",
    "SELECT '{S}.coll'::regcollation",
    "SELECT '{S}.cfg'::regconfig",
    "SELECT '{{{S}.cfg}}'::regconfig[]",
    "SELECT to_regclass('{S}.t')",
    "SELECT to_regtype('{S}.ty')",
    "SELECT to_regproc('{S}.f')",
    "SELECT to_regprocedure('{S}.f(int)')",
    "SELECT to_regnamespace('{S}')",
    "SELECT to_regoperator('{S}.===(int,int)')",
    "SELECT has_table_privilege('{S}.t', 'select')",
    "SELECT has_schema_privilege('{S}', 'usage')",
    "SELECT has_function_privilege('{S}.f(int)', 'execute')",
    "SELECT has_function_privilege('master.f({S}.t)', 'execute')",
    "SELECT has_sequence_privilege('{S}.s', 'usage')",
    "SELECT has_type_privilege('{S}.ty', 'usage')",
    "SELECT has_column_privilege('{S}.t', 'code', 'select')",
    "SELECT pg_get_serial_sequence('{S}.t', 'code')",
    "SELECT pg_input_is_valid('{S}.t', 'regclass')",
    "SELECT pg_input_is_valid('hidden_a', '{S}.e')",
    "SELECT currtid2('{S}.t', '(0,1)')",
    # ---- text search and collations
    "SELECT to_tsvector('{S}.cfg', 'x')",
    "SELECT to_tsquery('{S}.cfg', 'x')",
    "SELECT plainto_tsquery('{S}.cfg', 'x')",
    "SELECT ts_headline('{S}.cfg', 'x', 'x'::tsquery)",
    "SELECT ts_lexize('{S}.dict', 'x')",
    "SELECT ts_debug('{S}.cfg', 'x')",
    "SET default_text_search_config = '{S}.cfg'",
    "CREATE FUNCTION master.cf6() RETURNS int LANGUAGE sql SET default_text_search_config = '{S}.cfg' AS 'select 1'",
    "SELECT code COLLATE {S}.coll FROM master.t",
    "CREATE TABLE master.c12 (a text COLLATE {S}.coll)",
    "CREATE INDEX ON master.t (code COLLATE {S}.coll)",
    "CREATE INDEX ON master.t (code {S}.ops)",
    "DROP COLLATION {S}.coll",
    "COMMENT ON TEXT SEARCH CONFIGURATION {S}.cfg IS 'x'",
    "DROP TEXT SEARCH CONFIGURATION {S}.cfg",
    "CREATE TRIGGER tg2 BEFORE INSERT ON master.t FOR EACH ROW EXECUTE FUNCTION tsvector_update_trigger(name, '{S}.cfg', code)",
    # ---- GRANT / REVOKE / COMMENT / ALTER / DROP / SECURITY LABEL / DEFAULT PRIVILEGES
    "GRANT USAGE ON TYPE {S}.ty TO PUBLIC",
    "REVOKE USAGE ON TYPE {S}.ty FROM PUBLIC",
    "GRANT USAGE ON DOMAIN {S}.dom TO PUBLIC",
    "GRANT SELECT ON {S}.t TO PUBLIC",
    "GRANT SELECT ON ALL TABLES IN SCHEMA {S} TO PUBLIC",
    "GRANT USAGE ON SCHEMA {S} TO PUBLIC",
    "REVOKE ALL ON SCHEMA {S} FROM PUBLIC",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA {S} GRANT SELECT ON TABLES TO PUBLIC",
    "COMMENT ON TABLE {S}.t IS 'x'",
    "COMMENT ON COLUMN {S}.t.code IS 'x'",
    "COMMENT ON SCHEMA {S} IS 'x'",
    "COMMENT ON TYPE {S}.ty IS 'x'",
    "COMMENT ON SEQUENCE {S}.s IS 'x'",
    "COMMENT ON CONSTRAINT c ON {S}.t IS 'x'",
    "COMMENT ON TRIGGER tg ON {S}.t IS 'x'",
    "SECURITY LABEL ON TABLE {S}.t IS 'x'",
    "ALTER VIEW {S}.v RENAME TO w",
    "ALTER INDEX {S}.t_pkey RENAME TO x",
    "ALTER TYPE {S}.ty OWNER TO CURRENT_USER",
    "ALTER TYPE master.mt SET SCHEMA {S}",
    "ALTER FUNCTION master.mf() SET SCHEMA {S}",
    "DROP SCHEMA {S}",
    "ALTER SCHEMA {S} RENAME TO z",
    # ---- plpgsql bodies
    "DO $$ BEGIN PERFORM count(*) FROM {S}.t; END $$",
    "DO $$ DECLARE n int := (SELECT count(*) FROM {S}.t); BEGIN NULL; END $$",
    "DO $$ DECLARE a int[] := '{{}}'; BEGIN a[(SELECT ascii(name) FROM {S}.t LIMIT 1)] := 1; END $$",
    "DO $$ DECLARE r {S}.t%ROWTYPE; BEGIN NULL; END $$",
    "DO $$ DECLARE c {S}.t.code%TYPE; BEGIN NULL; END $$",
    "DO $$ DECLARE e {S}.e; BEGIN NULL; END $$",
    "DO $$ BEGIN RAISE NOTICE '%', (SELECT name FROM {S}.t LIMIT 1); END $$",
    "DO $$ BEGIN RAISE NOTICE 'x' USING HINT = (SELECT name FROM {S}.t LIMIT 1); END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM {S}.t) THEN NULL; END IF; END $$",
    "DO $$ BEGIN FOR i IN 1..(SELECT count(*) FROM {S}.t) LOOP NULL; END LOOP; END $$",
    "DO $$ DECLARE r record; BEGIN FOR r IN SELECT * FROM {S}.t LOOP NULL; END LOOP; END $$",
    "DO $$ BEGIN CALL {S}.p(); END $$",
    "DO $$ BEGIN PERFORM {S}.f(1); END $$",
    "DO $$ BEGIN PERFORM 'x'::{S}.e; END $$",
    "DO $$ BEGIN PERFORM '{S}.t'::regclass; END $$",
    "DO $$ BEGIN EXECUTE 'SELECT * FROM {S}.t'; END $$",
    "CREATE FUNCTION master.cf7() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN (SELECT count(*) FROM {S}.t); END $$",
    "CREATE FUNCTION master.cf8() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.name := {S}.f(1)::text; RETURN NEW; END $$",
    # ---- settings
    "SET search_path = {S}",
    "SET LOCAL search_path TO {S}, master",
    "DO $$ BEGIN SET/**/search_path = {S}; END $$",
    "SELECT set_config('search_path', '{S}', true)",
    "ALTER FUNCTION master.mf() SET search_path = {S}",
    # ---- refused families (the server would answer differently by existence)
    "CREATE AGGREGATE master.ag(int) (SFUNC = {S}.f3, STYPE = int)",
    "CREATE TEXT SEARCH CONFIGURATION master.cfg2 (COPY = {S}.cfg)",
    "CREATE STATISTICS master.st ON code, name FROM {S}.t",
    "CREATE OPERATOR CLASS master.oc FOR TYPE int USING btree AS OPERATOR 1 {S}.<",
    "ALTER TEXT SEARCH CONFIGURATION {S}.cfg ADD MAPPING FOR word WITH simple",
    # ---- data strings that merely look like names: allowed, identical
    "INSERT INTO master.t VALUES ('{S}.t', 'x')",
    "UPDATE master.t SET name = '{S}.something' WHERE false",
    "SELECT '{S}.t' AS label, code FROM master.t",
    "COMMENT ON TABLE master.t IS 'copied from {S}.t'",
]


def make_user(role, email, **grants):
    u = User.objects.create(username=email, email=email, role=role)
    for schema, level in grants.items():
        SchemaGrant.objects.create(user=u, schema=schema, role=level)
    return u


def build(*statements, as_admin=False, dbname=None):
    if as_admin:
        with pgsetup.connect(pgsetup.ADMIN, dbname) as c:
            for st in statements:
                c.execute(st)
        return
    with dbconn.rw_connection(autocommit=True) as c:
        for st in statements:
            c.execute(st)


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect(pgsetup.ADMIN, master_db) as su:
        for (s,) in su.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            su.execute(f'DROP SCHEMA "{s}" CASCADE')
        su.execute("DROP OPERATOR IF EXISTS master.<<< (int, int)")
        su.execute("DROP TYPE IF EXISTS master.mt CASCADE")


@pytest.fixture(autouse=True)
def fresh_settings():
    from config.siconfig import invalidate_overrides
    invalidate_overrides()
    yield
    invalidate_overrides()


@pytest.fixture
def world(master_db):
    """crm: a hidden database holding an object for every name position of the corpus."""
    from .test_executor_schemas import make_crm

    make_crm()
    build(
        "CREATE TABLE master.t (code text PRIMARY KEY, name text)",
        "INSERT INTO master.t VALUES ('M1', 'Alpha')",
        "CREATE SEQUENCE master.s0",
        "CREATE TYPE master.mt AS (a int)",
        "CREATE FUNCTION master.mf() RETURNS int LANGUAGE sql AS 'select 1'",
        "CREATE TABLE crm.t (code text PRIMARY KEY, name text)",
        "INSERT INTO crm.t VALUES ('C1', 'Secret Co')",
        "CREATE VIEW crm.v AS SELECT * FROM crm.t",
        "CREATE MATERIALIZED VIEW crm.mv AS SELECT * FROM crm.t",
        "CREATE TYPE crm.ty AS (a int)",
        "CREATE TYPE crm.e AS ENUM ('hidden_a', 'hidden_b')",
        "CREATE DOMAIN crm.dom AS int",
        "CREATE SEQUENCE crm.s",
        "CREATE FUNCTION crm.f(int) RETURNS int LANGUAGE sql IMMUTABLE AS 'select $1'",
        "CREATE FUNCTION crm.f2(int, int) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'select $1 = $2'",
        "CREATE FUNCTION crm.f3(int, int) RETURNS int LANGUAGE sql IMMUTABLE AS 'select $1 + $2'",
        "CREATE FUNCTION crm.tf() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        "CREATE PROCEDURE crm.p() LANGUAGE sql AS 'select 1'",
        "CREATE OPERATOR crm.=== (LEFTARG = int, RIGHTARG = int, FUNCTION = crm.f2)",
        "CREATE OPERATOR crm.= (LEFTARG = int, RIGHTARG = int, FUNCTION = crm.f2)",
        "CREATE OPERATOR crm.< (LEFTARG = text, RIGHTARG = text, FUNCTION = pg_catalog.text_lt)",
        "CREATE TEXT SEARCH CONFIGURATION crm.cfg (COPY = simple)",
        "CREATE TEXT SEARCH DICTIONARY crm.dict (TEMPLATE = simple)",
        "CREATE COLLATION crm.coll (locale = 'C')",
    )
    build("CREATE OPERATOR CLASS crm.ops FOR TYPE text USING btree AS OPERATOR 1 pg_catalog.<, "
          "FUNCTION 1 pg_catalog.bttextcmp(text, text)", as_admin=True, dbname=master_db)
    return {
        "editor": make_user(Role.EDITOR, "ed@x.com", master=Role.EDITOR),
        "viewer": make_user(Role.VIEWER, "vi@x.com", master=Role.VIEWER),
    }


def _norm(text: str, s: str) -> str:
    return str(text).replace(s, "S")


def raw_outcome(user, sql, s):
    try:
        ch = service.propose_sql(user, sql, REASON)
    except service.ChangeError as exc:
        return ("refused", exc.code, _norm(exc.message, s))
    return ("proposed", ch.status, [_norm(d, s) for d in ch.diff])


def query_outcome(user, sql, s):
    try:
        out = query.run_select(sql, user=user)
    except (query.QueryError, Exception) as exc:   # noqa: BLE001 - the text is what is compared
        return ("refused", type(exc).__name__, _norm(exc, s))
    return ("ran", _norm(out, s))


def test_corpus_is_large_enough():
    assert len(SHAPES) >= 150 and len(set(SHAPES)) == len(SHAPES)


@pytest.mark.parametrize("shape", SHAPES)
def test_hidden_database_answers_like_a_missing_one(world, shape):
    editor = world["editor"]
    hidden = raw_outcome(editor, shape.format(S="crm"), "crm")
    missing = raw_outcome(editor, shape.format(S="nope"), "nope")
    assert hidden == missing
    assert "Secret" not in str(hidden) and "hidden_a" not in str(hidden)
    if shape.lstrip().upper().startswith(("SELECT", "WITH")):
        viewer = world["viewer"]
        hq = query_outcome(viewer, shape.format(S="crm"), "crm")
        mq = query_outcome(viewer, shape.format(S="nope"), "nope")
        assert hq == mq
        assert "Secret" not in str(hq) and "hidden_a" not in str(hq)
