import pytest

from accounts.models import Role, User
from config.siconfig import get_config
from settings_app import tunables

pytestmark = pytest.mark.django_db


def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def test_raw_sql_defaults():
    get_config.cache_clear()
    cfg = get_config()
    assert cfg["limits"]["maxSqlBytes"] == 100000
    assert cfg["limits"]["maxSqlStatements"] == 50
    assert cfg["sql"]["enabled"] is True


def test_switch_can_be_turned_off_and_reset():
    a = approver()
    tunables.apply(a, {"sql.enabled": False})
    assert get_config()["sql"]["enabled"] is False
    tunables.apply(a, {"sql.enabled": None})
    assert get_config()["sql"]["enabled"] is True


@pytest.mark.parametrize("bad", ["no", 1, 0, None.__class__])
def test_switch_must_be_a_real_boolean(bad):
    with pytest.raises(tunables.TunableError):
        tunables.apply(approver(), {"sql.enabled": bad})


@pytest.mark.parametrize("key,value", [("limits.maxSqlStatements", 0), ("limits.maxSqlStatements", 501),
                                       ("limits.maxSqlBytes", 1023), ("limits.maxSqlBytes", 5000001)])
def test_sql_limits_are_bounded(key, value):
    with pytest.raises(tunables.TunableError):
        tunables.apply(approver(), {key: value})


def test_sql_limits_can_be_tuned():
    tunables.apply(approver(), {"limits.maxSqlStatements": 7})
    assert get_config()["limits"]["maxSqlStatements"] == 7


def test_describe_reports_the_switch_as_a_bool_kind():
    row = next(t for t in tunables.describe() if t["key"] == "sql.enabled")
    assert row["kind"] == "bool" and row["value"] is True
