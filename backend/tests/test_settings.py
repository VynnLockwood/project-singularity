import pytest

from accounts.models import Role, User
from config.siconfig import get_config
from settings_app import tunables
from settings_app.models import Setting

pytestmark = pytest.mark.django_db


def user(role):
    return User.objects.create(username=f"{role}@x.com", email=f"{role}@x.com", role=role)


def client_for(u):
    from django.test import Client
    c = Client()
    c.force_login(u)
    return c


def test_overrides_flow_into_get_config_and_reset_restores_the_file_value():
    get_config.cache_clear()
    default = get_config()["search"]["minConfidence"]
    tunables.apply(user(Role.APPROVER), {"search.minConfidence": 0.65})
    assert get_config()["search"]["minConfidence"] == 0.65
    tunables.apply(User.objects.get(role=Role.APPROVER), {"search.minConfidence": None})
    assert get_config()["search"]["minConfidence"] == default
    assert not Setting.objects.exists()


def test_validation_is_all_or_nothing():
    a = user(Role.APPROVER)
    with pytest.raises(tunables.TunableError) as e:
        tunables.apply(a, {"search.topK": 8, "duplicates.threshold": 0.2, "nope.x": 1, "chat.defaultModel": "evil/model"})
    assert len(e.value.errors) == 3
    assert not Setting.objects.exists()
    for bad in (True, "3", 2.5):
        with pytest.raises(tunables.TunableError):
            tunables.apply(a, {"search.topK": bad})


def test_only_approvers_change_settings_but_everyone_can_read():
    ed, ap = user(Role.EDITOR), user(Role.APPROVER)
    r = client_for(ed).patch("/api/settings", {"search.topK": 7}, content_type="application/json")
    assert r.status_code == 403
    assert client_for(ed).get("/api/settings").status_code == 200
    r = client_for(ap).patch("/api/settings", {"search.topK": 7}, content_type="application/json")
    assert r.status_code == 200
    topk = next(s for s in r.json()["settings"] if s["key"] == "search.topK")
    assert topk["value"] == 7 and topk["overridden"] is True and topk["default"] == 5


def test_bad_values_return_400_with_messages():
    r = client_for(user(Role.APPROVER)).patch("/api/settings", {"duplicates.threshold": 3}, content_type="application/json")
    assert r.status_code == 400 and "between" in r.json()["errors"][0]


def test_changes_are_audited():
    from audit.models import AuditLog as AuditEvent
    tunables.apply(user(Role.APPROVER), {"limits.maxQueryRows": 100})
    ev = AuditEvent.objects.filter(action="settings.updated").first()
    assert ev and ev.details["limits.maxQueryRows"]["to"] == 100


def test_unauthenticated_gets_401():
    from django.test import Client
    assert Client().get("/api/settings").status_code == 401
