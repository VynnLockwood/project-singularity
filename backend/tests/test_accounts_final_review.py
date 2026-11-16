"""Final whole-branch review fixes for admin management: seed bootstrap, migrate hook, live sockets on role or
access changes, capabilities that never break /api/me, the 0005 rollback and a null seedAdmins."""
import asyncio
import importlib

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.db import transaction
from django.utils import timezone

from accounts import services
from accounts.models import Role, User
from accounts.services import ensure_seed_admins
from chat import events
from chat.consumers import EventsConsumer

pytestmark = pytest.mark.django_db


def make(email, role=Role.VIEWER, active=True, signed_in=False):
    return User.objects.create(username=email, email=email, role=role, is_active=active,
                               last_login=timezone.now() if signed_in else None)


# ---- I1: seed bootstrap ---------------------------------------------------------------------------------------

def test_placeholder_seed_domains_are_skipped_with_a_warning(caplog):
    make("existing@example.org", Role.EDITOR)
    seeds = ["admin@example.com", "existing@example.org", "b@example.net", "c@corp.invalid", "d@box.test",
             "e@localhost", "ok@acme.co"]
    with caplog.at_level("WARNING", logger="accounts"):
        got = ensure_seed_admins({"seedAdmins": seeds})
    assert [u.email for u in got] == ["ok@acme.co"]
    assert not User.objects.filter(email="admin@example.com").exists()
    assert User.objects.get(email="existing@example.org").role == Role.EDITOR  # never promoted
    assert set(User.objects.values_list("email", flat=True)) == {"existing@example.org", "ok@acme.co"}
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "admin@example.com" in msg and "placeholder" in msg


def test_unclaimed_seed_admin_does_not_block_promoting_a_real_user():
    ensure_seed_admins({"seedAdmins": ["typo@acme.co"]})  # a typo: created as admin, nobody can ever sign in as it
    assert User.objects.get(email="typo@acme.co").role == Role.ADMIN
    make("real@acme.co", Role.VIEWER)  # the operator adds their real (existing) address to the list
    ensure_seed_admins({"seedAdmins": ["typo@acme.co", "real@acme.co"]})
    assert User.objects.get(email="real@acme.co").role == Role.ADMIN


def test_a_signed_in_admin_blocks_bootstrap():
    make("root@acme.co", Role.ADMIN, signed_in=True)
    make("boss@acme.co", Role.VIEWER)
    ensure_seed_admins({"seedAdmins": ["root@acme.co", "boss@acme.co"]})
    assert User.objects.get(email="boss@acme.co").role == Role.VIEWER


def test_a_seed_admin_who_has_signed_in_counts():
    make("seed@acme.co", Role.ADMIN, signed_in=True)
    make("other@acme.co", Role.EDITOR)
    ensure_seed_admins({"seedAdmins": ["seed@acme.co", "other@acme.co"]})
    assert User.objects.get(email="other@acme.co").role == Role.EDITOR


def test_an_admin_not_in_the_seed_list_counts_even_if_never_signed_in():
    make("invited@acme.co", Role.ADMIN)
    make("boss@acme.co", Role.APPROVER)
    ensure_seed_admins({"seedAdmins": ["boss@acme.co"]})
    assert User.objects.get(email="boss@acme.co").role == Role.APPROVER


# ---- I2: the post_migrate hook never aborts migrate ------------------------------------------------------------

def test_migrate_hook_swallows_and_logs_seed_errors(monkeypatch, caplog):
    from accounts.apps import seed_admins_after_migrate

    def boom(cfg):
        raise RuntimeError("seed exploded")

    monkeypatch.setattr("config.siconfig.get_config", lambda: {"auth": {"seedAdmins": ["x@acme.co"]}})
    monkeypatch.setattr("accounts.services.ensure_seed_admins", boom)
    with caplog.at_level("ERROR", logger="accounts"):
        seed_admins_after_migrate(sender=None, using="default")  # must not raise
    assert any("seed exploded" in (r.exc_text or "") or r.exc_info for r in caplog.records)


def test_migrate_hook_swallows_config_errors(monkeypatch):
    from accounts.apps import seed_admins_after_migrate

    def bad():
        raise ValueError("bad yaml")

    monkeypatch.setattr("config.siconfig.get_config", bad)
    seed_admins_after_migrate(sender=None, using="default")


# ---- I4: live sockets follow role and access changes ----------------------------------------------------------

def communicator(user):
    comm = WebsocketCommunicator(EventsConsumer.as_asgi(), "/ws/events")
    comm.scope["user"] = user
    return comm


def run(go):
    """asyncio.run, then close the DB connections the consumer opened in asgiref's worker thread (otherwise the
    test database cannot be dropped at the end of the session)."""
    from django.db import connections

    async def wrapped():
        try:
            return await go()
        finally:
            await sync_to_async(connections.close_all)()

    return asyncio.run(wrapped())


async def closed(comm, timeout=1):
    out = await comm.receive_output(timeout=timeout)
    return out["type"] == "websocket.close"


@pytest.mark.django_db(transaction=True)
def test_deactivation_closes_live_socket_and_reconnect_is_refused():
    admin = make("admin@acme.co", Role.ADMIN)
    approver = make("appr@acme.co", Role.APPROVER)

    async def go():
        comm = communicator(approver)
        assert (await comm.connect())[0]
        await sync_to_async(services.set_user_active)(admin, approver, False)
        was_closed = await closed(comm)
        again = communicator(approver)  # the stale in-memory user still says active: the server re-reads it
        ok, _ = await again.connect()
        return was_closed, ok

    was_closed, ok = run(go)
    assert was_closed and not ok


@pytest.mark.django_db(transaction=True)
def test_demotion_closes_socket_and_reconnect_leaves_approver_events():
    admin = make("admin@acme.co", Role.ADMIN)
    approver = make("appr@acme.co", Role.APPROVER)

    async def go():
        comm = communicator(approver)
        assert (await comm.connect())[0]
        await sync_to_async(services.change_role)(admin, approver, Role.EDITOR.value)
        was_closed = await closed(comm)
        fresh = await sync_to_async(User.objects.get)(pk=approver.pk)
        again = communicator(fresh)
        assert (await again.connect())[0]
        await sync_to_async(events.publish)([events.APPROVERS], {"type": "change"})
        nothing = await again.receive_nothing(timeout=0.3)
        await again.disconnect()
        return was_closed, nothing

    was_closed, nothing = run(go)
    assert was_closed and nothing


@pytest.mark.django_db(transaction=True)
def test_rolled_back_role_change_does_not_close_the_socket():
    admin = make("admin@acme.co", Role.ADMIN)
    approver = make("appr@acme.co", Role.APPROVER)

    def dry_run():
        with transaction.atomic():
            services.change_role(admin, approver, Role.EDITOR.value)
            transaction.set_rollback(True)

    async def go():
        comm = communicator(approver)
        assert (await comm.connect())[0]
        await sync_to_async(dry_run)()
        nothing = await comm.receive_nothing(timeout=0.3)
        await comm.disconnect()
        return nothing

    assert run(go)


@pytest.mark.django_db(transaction=True)
def test_consumer_rechecks_role_before_delivering_an_event():
    approver = make("appr@acme.co", Role.APPROVER)

    async def go():
        comm = communicator(approver)
        assert (await comm.connect())[0]
        await sync_to_async(User.objects.filter(pk=approver.pk).update)(role=Role.VIEWER)  # changed behind its back
        await sync_to_async(events.publish)([events.APPROVERS], {"type": "change", "secret": 1})
        return await closed(comm)

    assert run(go)


# ---- I5: capabilities never break /api/me ---------------------------------------------------------------------

def test_capabilities_survive_a_broken_tool_registry(monkeypatch, caplog):
    from accounts import capabilities
    from mcp_server.app import mcp

    monkeypatch.setattr(mcp, "_tool_manager", object())  # private API changed shape
    capabilities._warned = False
    with caplog.at_level("WARNING"):
        got = capabilities.build(make("v@acme.co"))
    assert got["mcpTools"] and all(t["description"] == "" for t in got["mcpTools"])
    assert got["roleSummary"]


def test_api_me_still_answers_with_a_broken_tool_registry(monkeypatch, client):
    from mcp_server.app import mcp

    monkeypatch.setattr(mcp, "_tool_manager", object())
    client.force_login(make("v@acme.co"))
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["capabilities"]["mcpTools"]


# ---- M2: 0005 can be rolled back -------------------------------------------------------------------------------

def test_admin_role_migration_reverse_downgrades_admins():
    from django.apps import apps
    from django.db.migrations import RunPython

    mod = importlib.import_module("accounts.migrations.0005_admin_role")
    run = [op for op in mod.Migration.operations if isinstance(op, RunPython)]
    assert run and run[0].reversible
    make("a@acme.co", Role.ADMIN)
    make("b@acme.co", Role.EDITOR)
    run[0].reverse_code(apps, None)
    assert dict(User.objects.values_list("email", "role")) == {"a@acme.co": "approver", "b@acme.co": "editor"}


# ---- M8: a null or odd seedAdmins does not break config load --------------------------------------------------

@pytest.mark.parametrize("value", ["", " ~", " [Boss@Acme.co, 5]", " boss@acme.co"])
def test_odd_seed_admins_do_not_crash_config_load(tmp_path, monkeypatch, value):
    from config import siconfig

    f = tmp_path / "s.yaml"
    f.write_text(f"auth:\n  seedAdmins:{value}\n")
    monkeypatch.setenv("SINGULARITY_CONFIG", str(f))
    siconfig.get_config.cache_clear()
    try:
        seeds = siconfig.get_config()["auth"]["seedAdmins"]
        if value in ("", " ~"):
            assert seeds == []
        assert ensure_seed_admins({"seedAdmins": seeds}) is not None  # and seeding copes with whatever is left
    finally:
        siconfig.get_config.cache_clear()
