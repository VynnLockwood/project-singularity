import pytest

from accounts.models import Role, User
from config.siconfig import get_config
from settings_app import tunables

pytestmark = pytest.mark.django_db


def approver():
    return User.objects.create(username="a@x.com", email="a@x.com", role=Role.APPROVER)


def test_script_limit_defaults():
    get_config.cache_clear()
    limits = get_config()["limits"]
    assert limits["maxScriptBytes"] == 52428800
    assert limits["maxScriptRows"] == 200000
    assert limits["maxInlineScriptBytes"] == 20480


def test_script_rows_can_be_tuned():
    tunables.apply(approver(), {"limits.maxScriptRows": 10})
    assert get_config()["limits"]["maxScriptRows"] == 10


def test_script_rows_zero_is_rejected():
    with pytest.raises(tunables.TunableError):
        tunables.apply(approver(), {"limits.maxScriptRows": 0})


def test_max_ops_per_script_default_and_tunable():
    get_config.cache_clear()
    assert get_config()["limits"]["maxOpsPerScript"] == 1000
    a = approver()
    a.username = "mo@x.com"
    a.save(update_fields=["username"])
    tunables.apply(a, {"limits.maxOpsPerScript": 5})
    assert get_config()["limits"]["maxOpsPerScript"] == 5
    with pytest.raises(tunables.TunableError):
        tunables.apply(a, {"limits.maxOpsPerScript": 0})
    with pytest.raises(tunables.TunableError):
        tunables.apply(a, {"limits.maxOpsPerScript": 100001})
    tunables.apply(a, {"limits.maxOpsPerScript": 1000})  # restore; same test DB persists across files
