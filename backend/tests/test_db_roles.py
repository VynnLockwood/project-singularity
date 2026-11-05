"""Runs db/init/01-schemas-roles.sh against a scratch database and checks what
each of the three DB roles can and cannot do (spec section 1 / section 7).

Needs a Postgres superuser. Skipped unless TEST_PG_ADMIN_USER is set, e.g.
  TEST_PG_ADMIN_USER=postgres DB_HOST=/tmp DB_PORT=5544
In CI the pgvector image is used; locally the vector extension may be missing,
so the test strips only that one CREATE EXTENSION line when it is unavailable.
"""
import os
import subprocess
import sys

import pytest

from . import pgsetup
from .pgsetup import ADMIN, HOST, PORT, REPO

DBNAME = "roles_test"

pytestmark = pytest.mark.skipif(not ADMIN, reason="TEST_PG_ADMIN_USER not set")


def connect(user, dbname=DBNAME):
    return pgsetup.connect(user, dbname)


def allowed(user, sql):
    """True if `sql` runs as `user`, False if Postgres denies it."""
    import psycopg

    with connect(user) as conn:
        try:
            conn.execute(sql)
            return True
        except psycopg.errors.InsufficientPrivilege:
            return False


@pytest.fixture(scope="module")
def db():
    pgsetup.create_scratch_db(DBNAME)
    # rw creates a table in master, as the gate's apply step would.
    with connect("singularity_master_rw") as rw:
        rw.execute("CREATE TABLE master.customer (id int PRIMARY KEY, name text)")
        rw.execute("INSERT INTO master.customer VALUES (1, 'Acme')")
    yield
    pgsetup.drop_db(DBNAME)


def test_ro_can_read_master_but_not_write(db):
    assert allowed("singularity_master_ro", "SELECT * FROM master.customer")
    assert not allowed("singularity_master_ro", "INSERT INTO master.customer VALUES (2, 'x')")
    assert not allowed("singularity_master_ro", "UPDATE master.customer SET name = 'x'")
    assert not allowed("singularity_master_ro", "DELETE FROM master.customer")
    assert not allowed("singularity_master_ro", "CREATE TABLE master.t (id int)")
    assert not allowed("singularity_master_ro", "DROP TABLE master.customer")


def test_ro_cannot_touch_singularity_schema(db):
    assert not allowed("singularity_master_ro", "CREATE TABLE singularity.t (id int)")


def test_rw_has_ddl_and_dml_on_master(db):
    assert allowed("singularity_master_rw", "INSERT INTO master.customer VALUES (3, 'y')")
    assert allowed("singularity_master_rw", "UPDATE master.customer SET name = 'z' WHERE id = 3")
    assert allowed("singularity_master_rw", "ALTER TABLE master.customer ADD COLUMN phone text")
    assert allowed("singularity_master_rw", "CREATE TABLE master.supplier (id int)")
    assert allowed("singularity_master_rw", "DROP TABLE master.supplier")


def test_rw_cannot_touch_singularity_schema(db):
    assert not allowed("singularity_master_rw", "CREATE TABLE singularity.t (id int)")
    assert not allowed("singularity_master_rw", "CREATE TABLE public.t (id int)")


def test_new_master_tables_are_readable_by_ro(db):
    with connect("singularity_master_rw") as rw:
        rw.execute("CREATE TABLE master.later (id int)")
    assert allowed("singularity_master_ro", "SELECT * FROM master.later")


def test_app_cannot_touch_master(db):
    assert not allowed("singularity_app", "SELECT * FROM master.customer")
    assert not allowed("singularity_app", "INSERT INTO master.customer VALUES (9, 'x')")
    assert not allowed("singularity_app", "CREATE TABLE master.t (id int)")


def test_app_can_create_in_singularity_but_not_public(db):
    assert allowed("singularity_app", "CREATE TABLE singularity.settings_probe (id int)")
    assert not allowed("singularity_app", "CREATE TABLE public.probe (id int)")


def test_other_roles_cannot_read_app_tables(db):
    assert not allowed("singularity_master_ro", "SELECT * FROM singularity.settings_probe")
    assert not allowed("singularity_master_rw", "SELECT * FROM singularity.settings_probe")


def test_extensions_installed(db):
    with connect(ADMIN) as admin:
        names = {r[0] for r in admin.execute("SELECT extname FROM pg_extension")}
    assert "pg_trgm" in names


def test_django_migrates_as_the_restricted_app_role(db):
    """The real production permission set must be enough for `manage.py migrate`."""
    env = {
        **os.environ,
        "DB_HOST": HOST,
        "DB_PORT": PORT,
        "DB_NAME": DBNAME,
        "DB_APP_USER": "singularity_app",
        "DB_APP_PASSWORD": pgsetup.PASSWORDS["singularity_app"],
        "DJANGO_DEBUG": "1",
    }
    backend = REPO / "backend"
    out = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=backend, env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    with connect(ADMIN) as admin:
        in_singularity = admin.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'singularity' AND tablename = 'accounts_user'"
        ).fetchone()[0]
        in_public = admin.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone()[0]
    assert in_singularity == 1
    assert in_public == 0
