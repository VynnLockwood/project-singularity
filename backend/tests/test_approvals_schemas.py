import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from schema import introspect, query

from . import pgsetup

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def _cleanup_schemas(master_db):
    yield
    with pgsetup.connect("singularity_master_rw", master_db) as rw:
        for (s,) in rw.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE r.rolname = 'singularity_master_rw' AND nspname <> 'master'"
        ).fetchall():
            rw.execute(f'DROP SCHEMA "{s}" CASCADE')


def user(email, role):
    return User.objects.create(username=email, email=email, role=role)


@pytest.fixture
def editor():
    return user("editor@example.com", Role.EDITOR)


@pytest.fixture
def approver():
    return user("approver@example.com", Role.APPROVER)


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


CUSTOMER = [{"op": "create_table", "name": "customer", "columns": [col("code", unique=True), col("name")]}]
INSERT = [{"op": "insert_rows", "table": "customer", "rows": [{"code": "C1", "name": "Acme"}]}]


def run(approver, ops, schema="master"):
    c = service.propose(approver, ops, source="web", schema=schema)
    assert c.status == "pending", (c.status, c.check_result)
    c = service.approve(approver, c.pk)
    assert c.status == "applied", c.error
    return c


def make_crm(approver):
    return run(approver, [{"op": "create_schema", "name": "crm"}])


def tables(schema):
    return [t["name"] for t in introspect.list_tables(schema)]


def test_default_schema_is_master(approver):
    c = service.propose(approver, CUSTOMER, source="web")
    assert c.schema == "master"
    assert service.serialize(c)["schema"] == "master"


def test_propose_in_crm_stores_schema_and_applies_there(approver):
    make_crm(approver)
    c = service.propose(approver, CUSTOMER, source="web", schema="crm")
    assert c.schema == "crm" and c.status == "pending" and c.fingerprint
    assert service.serialize(c)["schema"] == "crm"
    assert c.fingerprint == introspect.fingerprint(["customer"], "crm")
    c = service.approve(approver, c.pk)
    assert c.status == "applied"
    assert tables("crm") == ["customer"] and tables("master") == []
    assert any(p.name.startswith("crm-") for p in __import__("pathlib").Path(
        __import__("django.conf", fromlist=["settings"]).settings.BACKUP_DIR).iterdir())
    run(approver, INSERT, schema="crm")
    r = query.run_select("SELECT count(*) AS n FROM crm.customer")
    assert r["rows"][0][0] == 1


def test_master_change_unaffected_by_crm_changes(approver):
    make_crm(approver)
    c = service.propose(approver, CUSTOMER, source="web", schema="master")
    run(approver, CUSTOMER, schema="crm")          # same table name elsewhere
    assert service.approve(approver, c.pk).status == "applied"


def test_unmanaged_schema_is_refused(approver):
    for bad in ("singularity", "public", "nope"):
        with pytest.raises(service.ChangeError) as e:
            service.propose(approver, CUSTOMER, source="web", schema=bad)
        assert e.value.code == "invalid" and bad in e.value.message
    assert Change.objects.count() == 0


def test_rename_makes_pending_change_stale(approver):
    make_crm(approver)
    c = service.propose(approver, CUSTOMER, source="web", schema="crm")
    run(approver, [{"op": "rename_schema", "schema": "crm", "new_name": "sales"}])
    c = service.approve(approver, c.pk)
    assert c.status == "stale" and "crm" in c.error
    assert tables("sales") == []
    c = service.recheck(approver, c.pk)              # still no crm: stale again, no 500
    assert c.status == "stale" and "crm" in c.error


def test_drop_makes_pending_change_stale(approver):
    make_crm(approver)
    c = service.propose(approver, CUSTOMER, source="web", schema="crm")
    run(approver, [{"op": "drop_schema", "schema": "crm", "reason": "unused"}])
    assert service.approve(approver, c.pk).status == "stale"


def test_create_schema_waits_for_approver(editor, approver):
    c = service.propose(editor, [{"op": "create_schema", "name": "crm"}], source="web")
    assert c.status == "pending" and c.kind == "schema" and c.schema == "master"
    assert not service.can_decide(editor, c)
    with pytest.raises(service.ChangeError) as e:
        service.approve(editor, c.pk)
    assert e.value.code == "forbidden"
    c = service.approve(approver, c.pk)
    assert c.status == "applied"
    assert "crm" in [m["name"] for m in introspect.managed_schemas()]


def test_schema_change_goes_stale_when_another_schema_appears(approver):
    c = service.propose(approver, [{"op": "create_schema", "name": "crm"}], source="web")
    assert c.fingerprint
    run(approver, [{"op": "create_schema", "name": "hr"}])
    c = service.approve(approver, c.pk)
    assert c.status == "stale"
    c = service.recheck(approver, c.pk)
    assert c.status == "pending"
    assert service.approve(approver, c.pk).status == "applied"


def test_drop_schema_needs_reason_and_approver(editor, approver):
    make_crm(approver)
    c = service.propose(approver, [{"op": "drop_schema", "schema": "crm"}], source="web")
    assert c.status == "rejected"
    c = service.propose(editor, [{"op": "drop_schema", "schema": "crm", "reason": "unused"}], source="web")
    assert c.status == "pending" and not service.can_decide(editor, c)
    assert service.approve(approver, c.pk).status == "applied"
    assert "crm" not in [m["name"] for m in introspect.managed_schemas()]


def test_revert_stays_in_crm(approver):
    make_crm(approver)
    run(approver, CUSTOMER, schema="crm")
    ins = run(approver, INSERT, schema="crm")
    rev = service.revert(approver, ins.pk)
    assert rev.status == "applied" and rev.schema == "crm"
    assert query.run_select("SELECT count(*) AS n FROM crm.customer WHERE is_active")["rows"][0][0] == 0
    first = Change.objects.filter(schema="crm", kind="schema").order_by("id").first()
    rev = service.revert(approver, first.pk)
    assert rev.status == "applied" and rev.schema == "crm" and tables("crm") == []


@pytest.mark.django_db(transaction=True)
def test_migration_gives_existing_rows_master(approver):
    ex = MigrationExecutor(connection)
    ex.migrate([("approvals", "0001_initial")])
    try:
        old = ex.loader.project_state([("approvals", "0001_initial")]).apps
        U, C = old.get_model("accounts", "User"), old.get_model("approvals", "Change")
        u = U.objects.create(username="m@example.com", email="m@example.com", role=Role.EDITOR)
        c = C.objects.create(requester=u, source="web", kind="data", status="pending")
        ex = MigrationExecutor(connection)
        ex.migrate(ex.loader.graph.leaf_nodes())
        assert Change.objects.get(pk=c.pk).schema == "master"
    finally:
        ex = MigrationExecutor(connection)
        ex.migrate(ex.loader.graph.leaf_nodes())
