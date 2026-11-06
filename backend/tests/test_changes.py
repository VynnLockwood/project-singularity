import pytest

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from audit.models import AuditLog
from schema import introspect, query
from settings_app import services as rules_service
from settings_app.models import Rule

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


def user(email, role):
    return User.objects.create(username=email, email=email, role=role)


@pytest.fixture
def viewer():
    return user("viewer@example.com", Role.VIEWER)


@pytest.fixture
def editor():
    return user("editor@example.com", Role.EDITOR)


@pytest.fixture
def editor2():
    return user("editor2@example.com", Role.EDITOR)


@pytest.fixture
def approver():
    return user("approver@example.com", Role.APPROVER)


def col(name, type="text", **kw):
    return {"name": name, "type": type, **kw}


CREATE_CUSTOMER = [{"op": "create_table", "name": "customer", "columns": [col("customerCode", unique=True), col("name")]}]
INSERT_ACME = [{"op": "insert_rows", "table": "customer", "rows": [{"customerCode": "C1", "name": "Acme"}]}]


def tables():
    return [t["name"] for t in introspect.list_tables()]


def actions():
    return list(AuditLog.objects.order_by("id").values_list("action", flat=True))


def make_customer(approver):
    c = service.propose(approver, CREATE_CUSTOMER, source="web")
    return service.approve(approver, c.pk)


# ---- permission table ------------------------------------------------------

def test_viewer_cannot_propose(viewer):
    with pytest.raises(service.ChangeError) as e:
        service.propose(viewer, CREATE_CUSTOMER, source="web")
    assert e.value.code == "forbidden"
    assert Change.objects.count() == 0


def test_approver_self_approves_schema_change(approver):
    c = service.propose(approver, CREATE_CUSTOMER, source="web", prompt="make customers")
    assert c.status == "pending" and c.kind == "schema"
    assert tables() == []                              # nothing happens until approved
    c = service.approve(approver, c.pk)
    assert c.status == "applied" and c.decided_by == approver and c.applied_at
    assert tables() == ["customer"]
    assert c.reverse_operations == [{"op": "drop_table", "table": "customer", "reason": "revert"}]


def test_editor_schema_change_waits_for_an_approver(editor, editor2, approver):
    c = service.propose(editor, CREATE_CUSTOMER, source="mcp")
    assert c.status == "pending"
    for who in (editor, editor2):
        with pytest.raises(service.ChangeError) as e:
            service.approve(who, c.pk)
        assert e.value.code == "forbidden"
    assert tables() == []
    assert service.approve(approver, c.pk).status == "applied"
    assert tables() == ["customer"]


def test_editor_self_approves_data_change(editor, approver):
    make_customer(approver)
    c = service.propose(editor, INSERT_ACME, source="web")
    assert c.kind == "data"
    c = service.approve(editor, c.pk)
    assert c.status == "applied"
    assert query.run_select("SELECT name, \"createdBy\" FROM customer")["rows"] == [["Acme", "editor@example.com"]]


def test_other_editor_cannot_approve_someone_elses_data_change(editor, editor2, approver):
    make_customer(approver)
    c = service.propose(editor, INSERT_ACME, source="web")
    with pytest.raises(service.ChangeError) as e:
        service.approve(editor2, c.pk)
    assert e.value.code == "forbidden"
    assert service.approve(approver, c.pk).status == "applied"      # approvers may decide any change


def test_mixed_change_counts_as_schema(editor):
    ops = CREATE_CUSTOMER + INSERT_ACME
    assert service.propose(editor, ops, source="web").kind == "schema"


def test_queue_visibility(viewer, editor, editor2, approver):
    make_customer(approver)
    schema_change = service.propose(editor, [{"op": "add_column", "table": "customer", "column": col("email")}], source="web")
    data_change = service.propose(editor, INSERT_ACME, source="web")
    ids = lambda u: {c.pk for c in service.pending_for(u)}
    assert ids(approver) == {schema_change.pk, data_change.pk}
    assert ids(editor) == {data_change.pk}          # editors only see data changes they can decide
    assert ids(editor2) == set() and ids(viewer) == set()


# ---- the gate ---------------------------------------------------------------

def test_gate_blocks_and_nothing_is_applied(approver):
    bad = [{"op": "create_table", "name": "Bad_Name", "columns": [col("x")]}]
    c = service.propose(approver, bad, source="web")
    assert c.status == "rejected" and c.decision_note == "blocked by the rules gate"
    assert any(v["rule"] == "tableNaming" and v["severity"] == "block" for v in c.check_result["violations"])
    with pytest.raises(service.ChangeError) as e:
        service.approve(approver, c.pk)
    assert e.value.code == "bad_state"
    assert tables() == []
    assert "change.rejected_by_gate" in actions()


def test_malformed_operations_are_rejected_with_reasons(editor):
    c = service.propose(editor, [{"op": "drop_table", "table": "x"}], source="mcp")   # no reason
    assert c.status == "rejected" and any("reason" in e for e in c.check_result["errors"])


def test_semantic_errors_are_rejected(editor):
    c = service.propose(editor, [{"op": "add_column", "table": "ghost", "column": col("x")}], source="mcp")
    assert c.status == "rejected" and "ghost" in c.check_result["errors"][0]


def test_warnings_do_not_block_and_are_recorded(approver):
    c = service.propose(approver, [{"op": "create_table", "name": "country",
                                    "columns": [col("name"), col("regionCode")]}], source="web")
    assert c.status == "pending"
    assert {v["severity"] for v in c.check_result["violations"]} == {"warn"}
    assert c.diff[0].startswith("+ create table country")
    assert any("CREATE TABLE" in s for s in c.generated_sql)
    assert "country.id" in c.check_result["autoAdded"]


# ---- stale detection -------------------------------------------------------

def test_change_goes_stale_when_the_schema_moves(approver):
    make_customer(approver)
    first = service.propose(approver, [{"op": "add_column", "table": "customer", "column": col("email")}], source="web")
    other = service.propose(approver, [{"op": "add_column", "table": "customer", "column": col("fax")}], source="web")
    assert service.approve(approver, other.pk).status == "applied"
    stale = service.approve(approver, first.pk)
    assert stale.status == "stale" and stale.error
    cols = [c["name"] for c in introspect.describe_table("customer")["columns"]]
    assert "fax" in cols and "email" not in cols
    # a re-check against the new state makes it approvable again
    again = service.recheck(approver, first.pk)
    assert again.status == "pending"
    assert service.approve(approver, first.pk).status == "applied"
    assert "email" in [c["name"] for c in introspect.describe_table("customer")["columns"]]


def test_change_goes_stale_when_rules_change(approver):
    c = service.propose(approver, CREATE_CUSTOMER, source="web")
    rule = Rule.objects.get(name="noFloat")
    rules_service.update_rule(approver, rule, enabled=False)
    assert service.approve(approver, c.pk).status == "stale"
    assert tables() == []
    assert service.recheck(approver, c.pk).status == "pending"


def test_stale_when_a_competing_table_appears(approver, editor):
    a = service.propose(editor, CREATE_CUSTOMER, source="mcp")
    b = service.propose(approver, CREATE_CUSTOMER, source="web")
    assert service.approve(approver, b.pk).status == "applied"
    stale = service.approve(approver, a.pk)
    assert stale.status == "stale"
    assert service.recheck(approver, a.pk).status == "rejected"      # now "table already exists"


# ---- reject ----------------------------------------------------------------

def test_requester_can_withdraw_own_change(editor, editor2):
    c = service.propose(editor, CREATE_CUSTOMER, source="mcp")
    with pytest.raises(service.ChangeError):
        service.reject(editor2, c.pk)
    c = service.reject(editor, c.pk, note="changed my mind")
    assert c.status == "rejected" and c.decision_note == "changed my mind" and c.decided_by == editor


def test_approver_can_reject_any_pending_change(editor, approver):
    c = service.propose(editor, CREATE_CUSTOMER, source="mcp")
    assert service.reject(approver, c.pk, note="use the existing table").status == "rejected"
    assert tables() == []


def test_declined_change_cannot_be_rechecked(editor):
    c = service.propose(editor, CREATE_CUSTOMER, source="mcp")
    service.reject(editor, c.pk)
    with pytest.raises(service.ChangeError):
        service.recheck(editor, c.pk)


def test_cannot_approve_twice(approver):
    c = service.propose(approver, CREATE_CUSTOMER, source="web")
    service.approve(approver, c.pk)
    with pytest.raises(service.ChangeError) as e:
        service.approve(approver, c.pk)
    assert e.value.code == "bad_state"


def test_unknown_change(approver):
    with pytest.raises(service.ChangeError) as e:
        service.approve(approver, 9999)
    assert e.value.code == "not_found"


# ---- failures --------------------------------------------------------------

def test_database_refusal_marks_failed_and_changes_nothing(approver):
    make_customer(approver)
    service.approve(approver, service.propose(approver, [{"op": "insert_rows", "table": "customer", "rows": [
        {"customerCode": "C1", "name": "same"}, {"customerCode": "C2", "name": "same"}]}], source="web").pk)
    bad = service.propose(approver, [
        {"op": "create_table", "name": "other", "columns": [col("otherCode", unique=True)]},
        {"op": "add_unique", "table": "customer", "columns": ["name"]},
    ], source="web")
    assert bad.status == "pending"
    bad = service.approve(approver, bad.pk)
    assert bad.status == "failed" and "unique" in bad.error
    assert tables() == ["customer"]
    assert "change.failed" in actions()


def test_failed_backup_blocks_the_change(approver, settings, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    settings.BACKUP_DIR = str(blocker / "sub")          # cannot be created
    c = service.approve(approver, service.propose(approver, CREATE_CUSTOMER, source="web").pk)
    assert c.status == "failed" and "backup failed" in c.error
    assert tables() == []


def test_schema_change_writes_a_backup_data_change_does_not(approver, settings):
    import pathlib

    make_customer(approver)
    folder = pathlib.Path(settings.BACKUP_DIR)
    dumps = list(folder.glob("master-*-change-*.dump"))
    assert len(dumps) == 1
    service.approve(approver, service.propose(approver, INSERT_ACME, source="web").pk)
    assert len(list(folder.glob("master-*.dump"))) == 1
    assert "backup.created" in actions()


# ---- revert ----------------------------------------------------------------

def test_revert_data_change(approver):
    make_customer(approver)
    ins = service.approve(approver, service.propose(approver, INSERT_ACME, source="web").pk)
    rev = service.revert(approver, ins.pk)
    assert rev.status == "applied" and rev.source == "revert" and rev.reverts_id == ins.pk
    assert query.run_select('SELECT "isActive" FROM customer')["rows"] == [[False]]
    assert "change.reverted" in actions()


def test_revert_schema_change(approver):
    c = make_customer(approver)
    service.revert(approver, c.pk)
    assert tables() == []


def test_cannot_revert_twice(approver):
    c = make_customer(approver)
    service.revert(approver, c.pk)
    with pytest.raises(service.ChangeError) as e:
        service.revert(approver, c.pk)
    assert e.value.code == "bad_state"


def test_cannot_revert_a_drop(approver):
    make_customer(approver)
    drop = service.approve(approver, service.propose(
        approver, [{"op": "drop_table", "table": "customer", "reason": "test"}], source="web").pk)
    assert drop.status == "applied"
    with pytest.raises(service.ChangeError, match="cannot be undone"):
        service.revert(approver, drop.pk)


def test_only_approvers_revert(editor, approver):
    c = make_customer(approver)
    with pytest.raises(service.ChangeError) as e:
        service.revert(editor, c.pk)
    assert e.value.code == "forbidden"


def test_cannot_revert_pending_or_rejected(approver):
    c = service.propose(approver, CREATE_CUSTOMER, source="web")
    with pytest.raises(service.ChangeError):
        service.revert(approver, c.pk)


def test_revert_ignores_rules_but_still_checks_state(approver):
    # create a table whose name today's rules would reject (rule off at the time)
    naming = Rule.objects.get(name="tableNaming")
    rules_service.update_rule(approver, naming, enabled=False)
    service.approve(approver, service.propose(approver, [
        {"op": "create_table", "name": "Old_Name", "columns": [col("oldCode", unique=True)]}], source="web").pk)
    rules_service.update_rule(approver, naming, enabled=True)
    ren = service.approve(approver, service.propose(approver, [
        {"op": "rename_table", "table": "Old_Name", "new_name": "newName"}], source="web").pk)
    assert tables() == ["newName"]
    rev = service.revert(approver, ren.pk)       # would be blocked by tableNaming if rules applied
    assert rev.status == "applied" and tables() == ["Old_Name"]


def test_revert_fails_cleanly_when_state_moved(approver):
    c = make_customer(approver)
    service.approve(approver, service.propose(approver, [
        {"op": "rename_table", "table": "customer", "new_name": "client"}], source="web").pk)
    rev = service.revert(approver, c.pk)          # wants to drop 'customer', which no longer exists
    assert rev.status == "rejected" and rev.check_result["errors"]
    assert tables() == ["client"]


# ---- audit and serialization ----------------------------------------------

def test_full_audit_trail(editor, approver):
    c = service.propose(editor, CREATE_CUSTOMER, source="mcp", prompt="add customers")
    service.approve(approver, c.pk)
    trail = actions()
    for a in ("change.proposed", "change.approved", "backup.created", "change.applied"):
        assert a in trail
    applied = AuditLog.objects.get(action="change.applied")
    assert applied.actor_email == "approver@example.com" and applied.details["tables"] == ["customer"]


def test_serialize(editor):
    c = service.propose(editor, CREATE_CUSTOMER, source="mcp", prompt="p")
    data = service.serialize(c)
    assert data["status"] == "pending" and data["requester"] == "editor@example.com"
    assert data["operations"][0]["columns"][0]["name"] == "id"      # normalized ops include auto-added columns
    assert data["sql"] and data["diff"] and data["tables"] == ["customer"]
    assert "operations" not in service.serialize(c, detail=False)


# ---- rules service ---------------------------------------------------------

def test_default_rules_seeded_once(approver):
    assert Rule.objects.count() == 10
    assert rules_service.seed_default_rules() == 0


def test_only_approvers_edit_rules(editor):
    with pytest.raises(rules_service.RuleError):
        rules_service.update_rule(editor, Rule.objects.get(name="noFloat"), enabled=False)
    with pytest.raises(rules_service.RuleError):
        rules_service.create_rule(editor, name="x", type="business_key", params={})


def test_rule_edit_versioning_and_rollback(approver):
    rule = Rule.objects.get(name="tableNaming")
    assert rule.version == 1
    rules_service.update_rule(approver, rule, params={**rule.params, "style": "snake_case"}, severity="warn")
    rule.refresh_from_db()
    assert rule.version == 2 and rule.params["style"] == "snake_case" and rule.severity == "warn"
    rules_service.rollback_rule(approver, rule, 1)
    rule.refresh_from_db()
    assert rule.version == 3 and rule.params["style"] == "camelCase" and rule.severity == "block"
    assert [v.action for v in rule.versions.all()] == ["rolled_back", "updated", "created"]
    entry = AuditLog.objects.get(action="rule.updated")
    assert entry.details["before"]["severity"] == "block" and entry.details["after"]["severity"] == "warn"


def test_invalid_rule_edit_rejected_and_unchanged(approver):
    rule = Rule.objects.get(name="tableNaming")
    with pytest.raises(rules_service.RuleError):
        rules_service.update_rule(approver, rule, params={"target": "table", "style": "kebab"})
    with pytest.raises(rules_service.RuleError):
        rules_service.update_rule(approver, rule, name="renamed")
    rule.refresh_from_db()
    assert rule.version == 1 and rule.params["style"] == "camelCase"


def test_create_rule_and_hash(approver):
    h = rules_service.rules_hash()
    rules_service.create_rule(approver, name="noTmp", type="forbidden", params={"ops": ["drop_index"]}, severity="warn")
    assert rules_service.rules_hash() != h
    with pytest.raises(rules_service.RuleError):
        rules_service.create_rule(approver, name="noTmp", type="business_key", params={})
    with pytest.raises(rules_service.RuleError):
        rules_service.create_rule(approver, name="bad", type="naming", params={})


def test_no_op_edit_does_not_bump_version(approver):
    rule = Rule.objects.get(name="noFloat")
    rules_service.update_rule(approver, rule, enabled=True)
    rule.refresh_from_db()
    assert rule.version == 1


def test_naming_style_is_config_driven(approver):
    for name in ("tableNaming", "columnNaming"):
        r = Rule.objects.get(name=name)
        rules_service.update_rule(approver, r, params={**r.params, "style": "snake_case"})
    fk = Rule.objects.get(name="foreignKeyNaming")
    rules_service.update_rule(approver, fk, params={"style": "snake_case"})
    good = service.propose(approver, [{"op": "create_table", "name": "supplier_contact",
                                       "columns": [col("contact_code", unique=True)]}], source="web")
    assert good.status == "pending"
    bad = service.propose(approver, [{"op": "create_table", "name": "supplierContact",
                                      "columns": [col("contact_code", unique=True)]}], source="web")
    assert bad.status == "rejected"


def test_revert_of_large_bulk_load_uses_script_cap(approver):
    from gate import check as gcheck
    from schema import executor
    ops = [{"op": "create_table", "name": "big", "columns": [col("label")]}]
    service.approve(approver, service.propose(approver, ops, source="web").pk)
    st = introspect.snapshot()
    op = {"op": "bulk_insert", "table": "big", "columns": ["label"], "source": {"script_id": 1, "stream": "s"},
          "refs": [], "expected_rows": 1500}
    res = gcheck.run([op], st, rules_service_active(), actor=approver.email, allow_bulk=True)
    assert res.ok, res.errors
    r = executor.execute(res.operations, st, approver.email, rules=rules_service_active(),
                         row_source=lambda o: iter((str(i),) for i in range(1500)))
    orig = Change.objects.create(requester=approver, source="script", kind="data", status=Change.Status.APPLIED,
                                 original_operations=[op], operations=[op], reverse_operations=r.reverse_ops)
    rev = service.revert(approver, orig.pk)
    assert rev.status == Change.Status.APPLIED, rev.error
    assert query.run_select("SELECT count(*) FROM master.big WHERE is_active")["rows"][0][0] == 0


def rules_service_active():
    return rules_service.active_rules()
