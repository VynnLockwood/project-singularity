"""Introspection per managed schema, dump allow-list, non-pg_catalog type refusal."""
import pytest

from schema import dump_model, introspect
from schema.conn import rw_connection, ro_connection

pytestmark = pytest.mark.django_db


@pytest.fixture
def crm(master):
    with rw_connection(autocommit=True) as rw:
        rw.execute("SELECT singularity.create_managed_schema('crm')")
        yield rw
        rw.execute("DROP SCHEMA IF EXISTS crm CASCADE")


def test_managed_schemas_lists_master_and_crm(crm):
    crm.execute("CREATE TABLE master.a (id int)")
    crm.execute("CREATE TABLE crm.t (id int)")
    crm.execute("CREATE TABLE crm.u (id int)")
    got = introspect.managed_schemas()
    assert [g["name"] for g in got] == ["master", "crm"]
    assert [g["tables"] for g in got] == [1, 2]
    assert not {"singularity", "public"} & {g["name"] for g in got}


def test_snapshot_only_that_schema(crm):
    crm.execute("CREATE TABLE master.m (id int)")
    crm.execute("CREATE TABLE crm.t (id int)")
    assert set(introspect.snapshot("crm")) == {"t"}
    assert set(introspect.snapshot()) == {"m"}
    assert set(introspect.external_state(schema="crm")) == {"crm.t"}
    assert [t["name"] for t in introspect.list_tables("crm")] == ["t"]
    assert [t["name"] for t in introspect.graph("crm")["tables"]] == ["t"]


def test_same_name_fingerprints_differ(crm):
    crm.execute("CREATE TABLE master.t (id int)")
    crm.execute("CREATE TABLE crm.t (id int)")
    assert introspect.fingerprint(["t"], "master") == introspect.fingerprint(["t"], "crm")
    crm.execute("ALTER TABLE crm.t ADD COLUMN x text")
    assert introspect.fingerprint(["t"], "master") != introspect.fingerprint(["t"], "crm")


def test_describe_table_scoped(crm):
    crm.execute("CREATE TABLE master.only_m (id int)")
    assert introspect.describe_table("only_m", "crm") is None
    assert introspect.describe_table("only_m")["name"] == "only_m"


@pytest.mark.parametrize("name", ["public", "singularity", "pg_catalog", "information_schema", "nope"])
def test_require_managed_refuses(master, name):
    with pytest.raises(introspect.UnknownSchema):
        introspect.require_managed(name)
    with pytest.raises(introspect.UnknownSchema):
        introspect.snapshot(name)
    with pytest.raises(introspect.UnknownSchema):
        introspect.describe_table("x", name)


def test_allowed_schemas_includes_crm(crm):
    assert dump_model.allowed_schemas() == ["master", "crm"]


def test_enum_column_refused_on_export(crm):
    crm.execute("CREATE TYPE crm.mood AS ENUM ('a', 'b')")
    crm.execute("CREATE TABLE crm.p (id int, m crm.mood)")
    with ro_connection() as conn:
        with pytest.raises(dump_model.ExportError) as e:
            dump_model.read_model(conn, "crm")
    assert e.value.status == 422 and "mood" in str(e.value)


def test_builtin_types_still_export(crm):
    crm.execute("CREATE TABLE crm.p (id int, n numeric(5,2), tags text[], at timestamptz)")
    with ro_connection() as conn:
        assert dump_model.read_model(conn, "crm")["tables"][0]["name"] == "p"


def test_partitioned_tables_are_counted(crm):
    crm.execute("CREATE TABLE crm.pt (id int) PARTITION BY RANGE (id)")
    assert [m["tables"] for m in introspect.managed_schemas() if m["name"] == "crm"] == [1]


def _refused(schema="crm"):
    with ro_connection() as conn:
        with pytest.raises(dump_model.ExportError) as e:
            dump_model.read_model(conn, schema)
    assert e.value.status == 422
    return str(e.value)


def test_domain_column_refused(crm):
    crm.execute("CREATE DOMAIN crm.pos AS int CHECK (VALUE > 0)")
    crm.execute("CREATE TABLE crm.d (v crm.pos)")
    assert "pos" in _refused()


def test_array_of_enum_refused(crm):
    crm.execute("CREATE TYPE crm.mood AS ENUM ('a', 'b')")
    crm.execute("CREATE TABLE crm.d (v crm.mood[])")
    _refused()


@pytest.mark.parametrize("ext", ["citext", "vector"])
def test_extension_type_refused(crm, ext):
    from . import pgsetup
    with pgsetup.connect(pgsetup.ADMIN, "master_test") as admin:
        if not admin.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", (ext,)).fetchone():
            pytest.skip(f"extension {ext} is not available in the scratch cluster")
        # the init script may already have installed it (vector lives in `singularity`)
        installed = admin.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (ext,)).fetchone()
        if not installed:
            admin.execute(f"CREATE EXTENSION {ext} SCHEMA public")
        try:
            ns = admin.execute(
                "SELECT t.typnamespace::regnamespace::text FROM pg_type t WHERE t.typname = %s", (ext,)
            ).fetchone()[0]
            col = f"{ns}.{ext}" + ("(3)" if ext == "vector" else "")
            admin.execute(f"CREATE TABLE crm.x (v {col})")
            _refused()
        finally:
            admin.execute("DROP TABLE IF EXISTS crm.x")
            if not installed:
                admin.execute(f"DROP EXTENSION IF EXISTS {ext} CASCADE")


def test_text_array_exports(crm):
    crm.execute("CREATE TABLE crm.p (tags text[])")
    with ro_connection() as conn:
        cols = dump_model.read_model(conn, "crm")["tables"][0]["columns"]
    assert cols[0]["type"] == "text[]"


def test_external_state_unique_keys_per_schema(crm):
    crm.execute("CREATE TABLE master.t (id int PRIMARY KEY, code text UNIQUE)")
    crm.execute("CREATE TABLE crm.t (id int PRIMARY KEY)")
    assert introspect.external_state()["master.t"]["unique"] == [["id"], ["code"]] or \
        sorted(introspect.external_state()["master.t"]["unique"]) == [["code"], ["id"]]
    assert introspect.external_state(schema="crm")["crm.t"]["unique"] == [["id"]]


@pytest.mark.parametrize("bad", [None, 5, "Master", "PUBLIC"])
def test_require_managed_rejects_odd_values(master, bad):
    with pytest.raises(introspect.UnknownSchema):
        introspect.require_managed(bad)

