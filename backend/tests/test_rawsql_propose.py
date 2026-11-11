"""Raw SQL: proposing (plan C task 4)."""
import pytest

from accounts.models import Role, User
from approvals import service
from approvals.models import Change
from audit.models import AuditLog
from schema import introspect
from settings_app import tunables

from .ops_helpers import make_table

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]


@pytest.fixture(autouse=True)
def fresh_settings():
    from config.siconfig import invalidate_overrides
    invalidate_overrides()
    yield
    invalidate_overrides()


REASON = "fix the typo in column a"


def user(role, name=None):
    n = name or role
    return User.objects.create(username=f"{n}@x.com", email=f"{n}@x.com", role=role)


@pytest.fixture
def editor():
    return user(Role.EDITOR)


def propose(u, sql="UPDATE master.t SET a = 'x'", reason=REASON, **kw):
    return service.propose_sql(u, sql, reason, **kw)


def err(u, code, match, **kw):
    with pytest.raises(service.ChangeError, match=match) as e:
        propose(u, **kw)
    assert e.value.code == code
    assert Change.objects.count() == 0


def test_viewer_cannot_propose():
    make_table()
    err(user(Role.VIEWER), "forbidden", "role")


def test_reason_needs_ten_characters(editor):
    make_table()
    err(editor, "invalid", "10", reason="too short")


def test_unknown_schema_is_invalid(editor):
    err(editor, "invalid", "nope", schema="nope")


def test_switch_off_refuses(editor):
    make_table()
    tunables.apply(user(Role.APPROVER, "ap"), {"sql.enabled": False})
    err(editor, "invalid", "switched off")


def test_refused_statement_creates_no_change(editor):
    make_table()
    err(editor, "invalid", "statement 2.*not allowed", sql="SELECT 1; SET ROLE postgres")


def test_dry_run_error_creates_no_change(editor):
    make_table()
    err(editor, "invalid", "statement 1.*nope", sql="SELECT * FROM master.nope")


def test_a_valid_proposal_is_a_pending_sql_change(editor):
    make_table(rows=[{"a": "1"}, {"a": "2"}])
    sql = "ALTER TABLE master.t ADD COLUMN c int; UPDATE master.t SET a = 'x'"
    ch = propose(editor, sql)
    assert (ch.kind, ch.status, ch.source, ch.schema) == ("sql", "pending", "mcp", "master")
    assert ch.operations == [{"op": "raw_sql", "sql": sql, "reason": REASON}]
    assert ch.generated_sql == ["ALTER TABLE master.t ADD COLUMN c int", "UPDATE master.t SET a = 'x'"]
    assert ch.diff[0].startswith("ALTER TABLE") and ch.diff[1].startswith("UPDATE 2")
    first = ch.check_result["violations"][0]
    assert first["rule"] == "rawSql" and first["severity"] == "warn"
    assert first["message"] == "Rules were not checked: this is raw SQL."
    assert ch.reverse_operations is None and ch.prompt == REASON and ch.affected_tables == []
    assert ch.fingerprint == introspect.schema_fingerprint("master")
    assert AuditLog.objects.filter(action="change.proposed", details__kind="sql").exists()
    assert "c" not in introspect.snapshot("master")["t"]["columns"]


def test_warnings_are_added(editor):
    make_table()
    ch = propose(editor, "DELETE FROM t")
    msgs = [v["message"] for v in ch.check_result["violations"]]
    assert any("without a WHERE" in m for m in msgs) and any("without a schema" in m for m in msgs)


def test_only_an_approver_decides(editor):
    make_table()
    ch = propose(editor)
    other = user(Role.EDITOR, "other")
    assert not service.can_decide(editor, ch) and not service.can_decide(other, ch)
    assert service.can_decide(user(Role.APPROVER), ch)
