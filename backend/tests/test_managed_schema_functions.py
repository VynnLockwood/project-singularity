"""SECURITY DEFINER functions that let singularity_master_rw manage schemas
(spec section 3.2). Runs db/init/01-schemas-roles.sh against a scratch DB.
Skipped unless TEST_PG_ADMIN_USER is set (see pgsetup)."""
import re

import psycopg
import pytest

from . import pgsetup
from .pgsetup import ADMIN, REPO

DBNAME = "managed_fn_test"
RW, RO, APP = "singularity_master_rw", "singularity_master_ro", "singularity_app"

pytestmark = pytest.mark.skipif(not ADMIN, reason="TEST_PG_ADMIN_USER not set")


def connect(user):
    return pgsetup.connect(user, DBNAME)


@pytest.fixture(scope="module")
def db():
    pgsetup.create_scratch_db(DBNAME)
    yield
    pgsetup.drop_db(DBNAME)


@pytest.fixture
def rw(db):
    with connect(RW) as conn:
        yield conn
        # cleanup: drop anything the test created
        for (s,) in conn.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner "
            "WHERE r.rolname=%s AND nspname <> 'master'", (RW,)
        ).fetchall():
            conn.execute(f'DROP SCHEMA "{s}" CASCADE')


def call(conn, fn, *args):
    marks = ", ".join(["%s"] * len(args))
    return conn.execute(f"SELECT singularity.{fn}_managed_schema({marks})", args)


def exists(conn, name):
    return conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (name,)).fetchone() is not None


def test_rw_can_create_via_function(rw):
    call(rw, "create", "crm")
    owner = rw.execute(
        "SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE nspname='crm'"
    ).fetchone()[0]
    assert owner == RW
    rw.execute("CREATE TABLE crm.t(id int)")
    rw.execute("INSERT INTO crm.t VALUES (1)")
    with connect(RO) as ro:
        assert ro.execute("SELECT id FROM crm.t").fetchall() == [(1,)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute("INSERT INTO crm.t VALUES (2)")


def test_rw_cannot_create_schema_directly(rw):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rw.execute("CREATE SCHEMA x")
    for user in (APP, RO):
        with connect(user) as c:
            for fn, args in (("create", ("zz",)), ("rename", ("master", "zz")), ("drop", ("master",))):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    call(c, fn, *args)
    assert not exists(rw, "zz")


@pytest.mark.parametrize(
    "name", ["singularity", "public", "pg_foo", "information_schema", "Crm", "1a", "a-b", "", "a" * 64, "master", None]
)
def test_reserved_and_bad_names_refused(rw, name):
    with pytest.raises(psycopg.errors.DatabaseError):
        call(rw, "create", name)


def test_bad_name_error_codes(rw):
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "create", "public")
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "create", "master")
    call(rw, "create", "a" * 63)  # longest legal name


def test_rename(rw):
    call(rw, "create", "crm")
    rw.execute("CREATE TABLE crm.t(id int)")
    call(rw, "rename", "crm", "sales")
    assert not exists(rw, "crm") and exists(rw, "sales")
    with connect(RO) as ro:
        ro.execute("SELECT * FROM sales.t")  # existing grant survived
    rw.execute("CREATE TABLE sales.t2(id int)")  # default privileges survived the rename
    with connect(RO) as ro:
        ro.execute("SELECT * FROM sales.t2")
    for old in ("master", "singularity", "public"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            call(rw, "rename", old, "other")
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "rename", "nope", "other")
    call(rw, "create", "hr")
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "rename", "hr", "sales")
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "rename", "hr", "master")
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "rename", "hr", "Bad")
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "rename", "hr", "pg_x")


def test_drop_empty_only(rw):
    call(rw, "create", "tmp")
    call(rw, "drop", "tmp")
    assert not exists(rw, "tmp")
    for ddl in ("CREATE TABLE tmp.t(id int)", "CREATE SEQUENCE tmp.s", "CREATE VIEW tmp.v AS SELECT 1 AS x",
                "CREATE TYPE tmp.ty AS ENUM ('a')", "CREATE FUNCTION tmp.f() RETURNS int LANGUAGE sql AS 'SELECT 1'"):
        call(rw, "create", "tmp")
        rw.execute(ddl)
        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            call(rw, "drop", "tmp")
        assert exists(rw, "tmp")
        rw.execute("DROP SCHEMA tmp CASCADE")
    for name in ("master", "public", "singularity"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            call(rw, "drop", name)
    with pytest.raises(psycopg.errors.InvalidSchemaName):
        call(rw, "drop", "nope")


def _function_bodies(text):
    found = re.findall(r"CREATE OR REPLACE FUNCTION.*?\$\$;", text, flags=re.S)
    return [re.sub(r"\s+", " ", f).strip() for f in found]


def test_init_and_migration_agree():
    sh = _function_bodies((REPO / "db/init/01-schemas-roles.sh").read_text())
    sql = _function_bodies((REPO / "db/migrations/2026-11-09-managed-schemas.sql").read_text())
    assert len(sh) == 3
    assert sh == sql


def test_rw_has_no_database_create(db):
    with connect(RW) as c:
        assert c.execute("SELECT has_database_privilege(%s, current_database(), 'CREATE')", (RW,)).fetchone()[0] is False
