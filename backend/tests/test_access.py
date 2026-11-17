"""Per-database access: grants capped by the global role, hidden equals missing, affected databases of a change."""
import pytest

from accounts import access
from accounts.models import Role, SchemaGrant, User
from schema import introspect

pytestmark = pytest.mark.django_db
hidden = pytest.mark.no_default_access  # every test below but the last opts out of the test-suite hook

MANAGED = ["master", "crm", "finance"]


@pytest.fixture(autouse=True)
def managed(monkeypatch):
    """The managed databases, without a Postgres scratch schema: tests edit the list to add one."""
    names = list(MANAGED)
    monkeypatch.setattr(introspect, "managed_schemas",
                        lambda: [{"name": n, "tables": 0, "comment": None} for n in names])
    return names


def user(role=Role.EDITOR, name=None, **kw):
    name = name or f"{role}-{User.objects.count()}@example.com"
    return User.objects.create(username=name, email=name, role=role, **kw)


def grant(u, schema, role):
    return SchemaGrant.objects.create(user=u, schema=schema, role=role)


@hidden
def test_no_grant_means_no_access():
    u = user(Role.APPROVER)
    assert not SchemaGrant.objects.filter(user=u).exists()  # the test hook is off here
    for s in MANAGED:
        assert access.effective_role(u, s) is None
        assert not access.can(u, s, Role.VIEWER)
    assert access.readable_schemas(u) == []


@hidden
def test_star_grant_reaches_every_database_including_new_ones(managed):
    u = user(Role.EDITOR)
    grant(u, access.ALL, Role.EDITOR)
    assert access.readable_schemas(u) == ["master", "crm", "finance"]
    managed.append("brand_new")
    assert access.effective_role(u, "brand_new") == Role.EDITOR
    assert access.can(u, "brand_new", Role.EDITOR)
    assert not access.can(u, "brand_new", Role.APPROVER)
    assert "brand_new" in access.readable_schemas(u)


@hidden
def test_specific_grant_overrides_star_both_ways():
    down = user(Role.APPROVER)
    grant(down, access.ALL, Role.EDITOR)
    grant(down, "crm", Role.VIEWER)
    assert access.effective_role(down, "crm") == Role.VIEWER
    assert access.effective_role(down, "finance") == Role.EDITOR

    up = user(Role.APPROVER)
    grant(up, access.ALL, Role.VIEWER)
    grant(up, "crm", Role.EDITOR)
    assert access.effective_role(up, "crm") == Role.EDITOR
    assert access.effective_role(up, "finance") == Role.VIEWER


@hidden
def test_grant_is_capped_by_global_role():
    u = user(Role.VIEWER)
    grant(u, access.ALL, Role.APPROVER)
    grant(u, "crm", Role.EDITOR)
    assert access.effective_role(u, "master") == Role.VIEWER
    assert access.effective_role(u, "crm") == Role.VIEWER
    assert not access.can(u, "crm", Role.EDITOR)
    # lowering the global role lowers access; the stored grant keeps its value
    e = user(Role.APPROVER)
    grant(e, access.ALL, Role.APPROVER)
    User.objects.filter(pk=e.pk).update(role=Role.EDITOR)
    e.refresh_from_db()
    assert access.effective_role(e, "master") == Role.EDITOR
    assert SchemaGrant.objects.get(user=e).role == Role.APPROVER


@hidden
def test_inactive_user_has_no_access():
    u = user(Role.APPROVER, is_active=False)
    grant(u, access.ALL, Role.APPROVER)
    assert access.effective_role(u, "master") is None
    assert access.readable_schemas(u) == []
    with pytest.raises(introspect.UnknownSchema):
        access.require(u, "master")
    admin = user(Role.ADMIN, is_active=False)
    assert access.effective_role(admin, "master") is None
    assert access.effective_role(None, "master") is None


@hidden
def test_admin_reaches_everything_without_grants(managed):
    a = user(Role.ADMIN)
    assert not SchemaGrant.objects.filter(user=a).exists()
    managed.append("later")
    for s in managed:
        assert access.effective_role(a, s) == Role.ADMIN
        assert access.can(a, s, Role.APPROVER)
        assert access.require(a, s, Role.APPROVER) == s
    assert access.readable_schemas(a) == ["master", "crm", "finance", "later"]


@hidden
def test_require_hidden_equals_missing():
    u = user(Role.APPROVER)
    grant(u, "crm", Role.APPROVER)
    assert access.require(u, "crm") == "crm"
    with pytest.raises(introspect.UnknownSchema) as hidden:
        access.require(u, "finance", Role.VIEWER)
    with pytest.raises(introspect.UnknownSchema) as missing:
        access.require(u, "nope", Role.VIEWER)
    with pytest.raises(introspect.UnknownSchema) as real_missing:
        introspect.require_managed("finance_x")
    assert str(hidden.value) == "Unknown schema 'finance'."
    assert str(missing.value) == "Unknown schema 'nope'."
    assert str(real_missing.value) == "Unknown schema 'finance_x'."
    # the same shape even at a higher level asked for: never a "forbidden" for a hidden database
    with pytest.raises(introspect.UnknownSchema) as hidden_high:
        access.require(u, "finance", Role.APPROVER)
    assert type(hidden_high.value) is type(missing.value)
    # a `*` grant still does not make a missing database exist
    star = user(Role.EDITOR)
    grant(star, access.ALL, Role.EDITOR)
    with pytest.raises(introspect.UnknownSchema) as star_missing:
        access.require(star, "nope")
    assert str(star_missing.value) == "Unknown schema 'nope'."


@hidden
def test_require_denies_level_with_role_message():
    u = user(Role.APPROVER)
    grant(u, "crm", Role.VIEWER)
    assert access.require(u, "crm", Role.VIEWER) == "crm"
    with pytest.raises(access.AccessDenied) as e:
        access.require(u, "crm", Role.EDITOR)
    assert e.value.status == 403
    msg = str(e.value)
    assert "editor" in msg and "'crm'" in msg and "viewer" in msg
    assert not isinstance(e.value, introspect.UnknownSchema)


@hidden
def test_readable_schemas_lists_only_granted():
    u = user(Role.EDITOR)
    grant(u, "finance", Role.VIEWER)
    grant(u, "master", Role.EDITOR)
    grant(u, "gone_db", Role.EDITOR)  # a grant for a database that no longer exists is ignored
    assert access.readable_schemas(u) == ["master", "finance"]


@hidden
def test_affected_schemas_for_each_database_op():
    a = access.affected_schemas
    assert a([{"op": "create_schema", "name": "crm2"}]) == {"crm2"}
    assert a([{"op": "rename_schema", "schema": "crm", "new_name": "sales"}]) == {"crm", "sales"}
    assert a([{"op": "drop_schema", "schema": "crm", "reason": "old"}]) == {"crm"}
    assert a([{"op": "move_table", "table": "t", "from_schema": "master", "to_schema": "crm"}]) == {"master", "crm"}
    assert a([{"op": "add_column", "table": "t", "column": {"name": "x", "type": "text"}}]) == {"master"}
    assert a([{"op": "add_column", "table": "t"}], default="crm") == {"crm"}
    assert a([{"op": "insert_rows", "schema": "finance", "table": "t", "rows": []}]) == {"finance"}
    assert a([{"op": "insert_rows", "table": "t", "rows": []},
              {"op": "drop_schema", "schema": "old", "reason": "x"}], default="crm") == {"crm", "old"}


@hidden
@pytest.mark.django_db(transaction=True)
def test_migration_gives_existing_non_admin_users_star_grant_at_global_role():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("accounts", "0006_email_ci_unique")]
    after = [("accounts", "0007_schema_grants")]
    ex = MigrationExecutor(connection)
    ex.migrate(before)
    try:
        OldUser = ex.loader.project_state(before).apps.get_model("accounts", "User")
        for r in ("viewer", "editor", "approver", "admin"):
            OldUser.objects.create(username=f"m-{r}@example.com", email=f"m-{r}@example.com", role=r)
        OldUser.objects.create(username="off@example.com", email="off@example.com", role="editor", is_active=False)
        ex = MigrationExecutor(connection)
        ex.migrate(after)
        got = {(g.user.email, g.schema, g.role) for g in SchemaGrant.objects.select_related("user")}
        assert got == {("m-viewer@example.com", "*", "viewer"), ("m-editor@example.com", "*", "editor"),
                       ("m-approver@example.com", "*", "approver"), ("off@example.com", "*", "editor")}
        # reversible: going back removes the grants table, forward again recreates the grants
        ex = MigrationExecutor(connection)
        ex.migrate(before)
        ex = MigrationExecutor(connection)
        ex.migrate(after)
        assert SchemaGrant.objects.count() == 4
    finally:
        ex = MigrationExecutor(connection)
        ex.migrate(ex.loader.graph.leaf_nodes())


def test_suite_hook_gives_new_non_admin_users_a_star_grant_at_their_role():
    """Outside `no_default_access`, users made by the older tests keep today's reach (tests/conftest.py)."""
    from .mcp_helpers import make_users

    people = make_users()
    for r, u in people.items():
        assert [(g.schema, g.role) for g in u.schema_grants.all()] == [(access.ALL, r)]
        assert access.effective_role(u, "crm") == r
    a = user(Role.ADMIN)
    assert not a.schema_grants.exists()
    people[Role.VIEWER].save()  # saving again adds nothing
    assert people[Role.VIEWER].schema_grants.count() == 1
