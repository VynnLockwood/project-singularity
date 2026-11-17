"""Per-database access on lists that span databases and on objects fetched by id (Task 5).

Search, duplicates, scripts, staged loads, import runs, intelligent query and the audit log. A list filters to
the databases the user can reach inside the query (before any top-k or limit), and an object whose database the
user cannot reach answers exactly like an id that does not exist.
"""
import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts import access
from accounts.models import Role, SchemaGrant, User
from approvals import service
from audit.models import AuditLog, entries, record
from duplicates.models import DuplicateGroup
from intake.models import ImportRun
from iquery.models import QueryExample
from mcp_server import tools
from scripts import storage
from scripts.models import Script, StagedLoad
from search.models import SchemaEmbedding

from .conftest import fake_vector
from .test_access_reads import _cleanup_schemas, admin, dbs, grant, make_user  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master"), pytest.mark.no_default_access]

HIDDEN, MISSING = "crm", "nope"
MISSING_ID = 987654


def client(user):
    c = Client()
    c.force_login(user)
    return c


def post(c, url, body=None):
    return c.post(url, data=json.dumps(body if body is not None else {}), content_type="application/json")


def no_grant(u):
    return grant(u, "master", Role.APPROVER)


def star_with_deny(u):
    grant(u, access.ALL, Role.APPROVER)
    return grant(u, HIDDEN, access.NONE)


SETUPS = {"no_grant": no_grant, "star_with_deny": star_with_deny}


@pytest.fixture(params=list(SETUPS))
def outsider(request, dbs):
    """A global approver (the most a non-admin can be) who cannot reach crm."""
    u = make_user(Role.APPROVER)
    SETUPS[request.param](u)
    return u


@pytest.fixture
def other(dbs):
    """Another person, an editor everywhere: the owner of the objects in crm."""
    return grant(make_user(Role.EDITOR, "other@x.com"), access.ALL, Role.EDITOR)


# ---- objects in the hidden database -------------------------------------------------------------

def _later():
    return timezone.now() + timedelta(hours=1)


def make_group(schema=HIDDEN, status="open"):
    return DuplicateGroup.objects.create(schema=schema, table_name="t", fields=["name"], key=f"k-{schema}-{status}-"
                                         f"{DuplicateGroup.objects.count()}", member_ids=[1, 2], score=0.9,
                                         status=status)


def make_script(owner, schema=HIDDEN, validated=True):
    s = storage.save(owner, "s.sql", f"COPY {schema}.t (code, name) FROM stdin;\nC9\tZed\n\\.\n".encode())
    if validated:
        s.status = Script.Status.VALIDATED
        s.report = {"ok": True, "schema": schema, "validated_schema": schema}
        s.save(update_fields=["status", "report"])
    return s


def make_load(owner, schema=HIDDEN):
    return StagedLoad.objects.create(owner=owner, schema=schema, table="t", columns=["code", "name"],
                                     path="none.copy", expires_at=_later())


def make_run(owner, schema=HIDDEN):
    return ImportRun.objects.create(owner=owner, schema=schema, prompt="p", file_path="/nonexistent/f.csv",
                                    file_name="f.csv", state=ImportRun.State.READY, profile={"x": 1},
                                    expires_at=_later())


def make_example(schema=HIDDEN):
    return QueryExample.objects.create(schema=schema, comments="c", sql="SELECT 1", sql_norm="select 1",
                                       embedding=fake_vector("c"))


MAKERS = {
    "group": lambda other: make_group().pk,
    "script": lambda other: make_script(other).pk,
    "load": lambda other: make_load(other).pk,
    "run": lambda other: make_run(other).pk,
    "example": lambda other: make_example().pk,
}

WEB = {
    "duplicate_detail": ("group", lambda c, i: c.get(f"/api/duplicates/{i}")),
    "duplicate_dismiss": ("group", lambda c, i: post(c, f"/api/duplicates/{i}/dismiss")),
    "duplicate_reopen": ("group", lambda c, i: post(c, f"/api/duplicates/{i}/reopen")),
    "duplicate_merge": ("group", lambda c, i: post(c, f"/api/duplicates/{i}/merge", {"survivorId": 1})),
    "script_detail": ("script", lambda c, i: c.get(f"/api/scripts/{i}")),
    "script_validate": ("script", lambda c, i: post(c, f"/api/scripts/{i}/validate", {})),
    "script_validate_async": ("script", lambda c, i: post(c, f"/api/scripts/{i}/validate?async=1", {})),
    "script_submit": ("script", lambda c, i: post(c, f"/api/scripts/{i}/submit", {})),
    "import_detail": ("run", lambda c, i: c.get(f"/api/imports/{i}")),
    "import_cancel": ("run", lambda c, i: c.delete(f"/api/imports/{i}")),
    "import_answer": ("run", lambda c, i: post(c, f"/api/imports/{i}/answer", {"text": "yes"})),
    "import_recheck": ("run", lambda c, i: post(c, f"/api/imports/{i}/recheck", {"mapping": {"tables": {}}})),
    "import_submit": ("run", lambda c, i: post(c, f"/api/imports/{i}/submit", {})),
    "example_delete": ("example", lambda c, i: c.delete(f"/api/intelligent-query/examples/{i}")),
}

MCP = {
    "get_duplicate": ("group", lambda u, i: tools.get_duplicate(u, i)),
    "merge_duplicates": ("group", lambda u, i: tools.merge_duplicates(u, i, 1)),
    "dismiss_duplicate": ("group", lambda u, i: tools.dismiss_duplicate(u, i)),
    "reopen_duplicate": ("group", lambda u, i: tools.reopen_duplicate(u, i)),
    "get_script": ("script", lambda u, i: tools.get_script(u, i)),
    "validate_script": ("script", lambda u, i: tools.validate_script(u, i)),
    "get_load": ("load", lambda u, i: tools.get_load(u, i)),
    "append_rows": ("load", lambda u, i: tools.append_rows(u, i, 1, "C1,Acme\n")),
    "finish_load": ("load", lambda u, i: tools.finish_load(u, i)),
    "cancel_load": ("load", lambda u, i: tools.cancel_load(u, i)),
}


def _norm(i):
    """What to replace: an id as it appears in messages (`#12`), or a database name."""
    return (f"#{i}", "#<id>") if isinstance(i, int) else (i, "<db>")


def _web_answer(resp, i):
    return resp.status_code, resp.content.decode().replace(*_norm(i))


def _mcp_answer(fn, i):
    try:
        out = ("ok", fn())
    except tools.ToolFailure as exc:
        out = ("fail", str(exc))
    return json.loads(json.dumps(out, default=str).replace(*_norm(i)))


@pytest.mark.parametrize("case", list(WEB))
def test_by_id_endpoints_hidden_equals_missing(outsider, other, case):
    kind, call = WEB[case]
    hid = MAKERS[kind](other)
    c = client(outsider)
    hidden = _web_answer(call(c, hid), hid)
    missing = _web_answer(call(c, MISSING_ID), MISSING_ID)
    assert hidden == missing
    assert hidden[0] in (400, 404, 409)


@pytest.mark.parametrize("case", list(MCP))
def test_by_id_tools_hidden_equals_missing(outsider, other, case):
    kind, call = MCP[case]
    hid = MAKERS[kind](other)
    hidden = _mcp_answer(lambda: call(outsider, hid), hid)
    missing = _mcp_answer(lambda: call(outsider, MISSING_ID), MISSING_ID)
    assert hidden == missing and hidden[0] == "fail"


def test_every_by_id_case_is_covered():
    """The brief's by-id endpoints and tools: a new one must be added here, not forgotten."""
    assert set(WEB) == {"duplicate_detail", "duplicate_dismiss", "duplicate_reopen", "duplicate_merge",
                        "script_detail", "script_validate", "script_validate_async", "script_submit",
                        "import_detail", "import_cancel", "import_answer", "import_recheck", "import_submit",
                        "example_delete"}
    assert set(MCP) == {"get_duplicate", "merge_duplicates", "dismiss_duplicate", "reopen_duplicate",
                        "get_script", "validate_script", "get_load", "append_rows", "finish_load", "cancel_load"}


def test_own_load_and_run_in_a_database_lost_are_hidden(dbs):
    u = grant(make_user(Role.EDITOR), "master", Role.EDITOR)
    load, run = make_load(u), make_run(u)
    assert _mcp_answer(lambda: tools.get_load(u, load.pk), load.pk) == \
        _mcp_answer(lambda: tools.get_load(u, MISSING_ID), MISSING_ID)
    c = client(u)
    assert _web_answer(c.get(f"/api/imports/{run.pk}"), run.pk) == \
        _web_answer(c.get(f"/api/imports/{MISSING_ID}"), MISSING_ID)


def test_by_id_writes_need_editor_on_that_database(dbs, other):
    """Readable but only viewer there: the role refusal (403 / role text), never the not-found answer."""
    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    g, load, run = make_group(), make_load(u), make_run(u)
    c = client(u)
    for url, body in ((f"/api/duplicates/{g.pk}/dismiss", {}), (f"/api/duplicates/{g.pk}/merge", {"survivorId": 1}),
                      (f"/api/imports/{run.pk}/answer", {"text": "x"}), (f"/api/imports/{run.pk}/submit", {})):
        r = post(c, url, body)
        assert r.status_code == 403 and "editor role on 'crm'" in r.json()["error"], (url, r.content)
    assert c.get(f"/api/duplicates/{g.pk}").status_code in (200, 404)   # viewer reads (404 only if unreadable rows)
    assert c.get(f"/api/imports/{run.pk}").status_code == 200
    for fn in (lambda: tools.dismiss_duplicate(u, g.pk), lambda: tools.reopen_duplicate(u, g.pk),
               lambda: tools.merge_duplicates(u, g.pk, 1), lambda: tools.append_rows(u, load.pk, 1, "C1,Acme\n"),
               lambda: tools.finish_load(u, load.pk), lambda: tools.cancel_load(u, load.pk)):
        with pytest.raises(tools.ToolFailure, match="editor role on 'crm'"):
            fn()
    assert tools.get_load(u, load.pk)["schema"] == HIDDEN
    assert DuplicateGroup.objects.get(pk=g.pk).status == "open"


def test_others_load_and_run_need_approver_on_that_database(dbs, other):
    u = grant(grant(make_user(Role.APPROVER), "master", Role.APPROVER), HIDDEN, Role.EDITOR)
    load, run = make_load(other), make_run(other)
    with pytest.raises(tools.ToolFailure, match="approver role on 'crm'"):
        tools.get_load(u, load.pk)
    r = client(u).get(f"/api/imports/{run.pk}")
    assert r.status_code == 403 and "approver role on 'crm'" in r.json()["error"]
    grant(u, HIDDEN, Role.APPROVER)
    assert tools.get_load(u, load.pk)["load_id"] == load.pk
    assert client(u).get(f"/api/imports/{run.pk}").status_code == 200


# ---- search ---------------------------------------------------------------------------------------

def _doc(schema, table, text):
    SchemaEmbedding.objects.create(schema=schema, kind="table", table_name=table, column_name="", content=text,
                                   content_hash=table, embedding=fake_vector(text))


@pytest.fixture
def search_docs(dbs, embed):
    for n in range(4):   # the best matches are all in the hidden database
        _doc(HIDDEN, f"customer_phone_{n}", "customer phone number contact")
    for n in range(4):
        _doc("master", f"thing_{n}", f"customer thing {n}")


def test_search_star_only_returns_readable_and_keeps_topk_full(search_docs):
    from search import service as search_service

    u = grant(make_user(Role.VIEWER), "master", Role.VIEWER)
    out = search_service.search("customer phone number", 3, None, user=u)
    assert len(out["results"]) == 3 and {r["schema"] for r in out["results"]} == {"master"}
    everything = search_service.search("customer phone number", 3, None)   # no user: as before (indexer, tests)
    assert {r["schema"] for r in everything["results"]} == {HIDDEN}
    r = client(u).get("/api/search", {"q": "customer phone number", "k": 3, "schema": "*"})
    assert r.status_code == 200 and [x["schema"] for x in r.json()["results"]] == ["master"] * 3
    tool = tools.search_schema(u, "customer phone number", 3, "*")
    assert [x["schema"] for x in tool["results"]] == ["master"] * 3


def test_search_one_hidden_database_answers_like_a_missing_one(search_docs):
    u = grant(make_user(Role.VIEWER), "master", Role.VIEWER)
    c = client(u)
    one = c.get("/api/search", {"q": "customer", "schema": HIDDEN})
    two = c.get("/api/search", {"q": "customer", "schema": MISSING})
    assert (one.status_code, one.content.decode().replace(HIDDEN, "<db>")) == \
        (two.status_code, two.content.decode().replace(MISSING, "<db>")) and one.status_code == 400
    assert _mcp_answer(lambda: tools.search_schema(u, "customer", 3, HIDDEN), HIDDEN) == \
        _mcp_answer(lambda: tools.search_schema(u, "customer", 3, MISSING), MISSING)


# ---- duplicates -----------------------------------------------------------------------------------

def test_duplicates_counts_do_not_leak_hidden(dbs):
    u = grant(make_user(Role.VIEWER), "master", Role.VIEWER)
    mine = make_group("master")
    for _ in range(3):
        make_group(HIDDEN)
    body = client(u).get("/api/duplicates", {"schema": "*"}).json()
    assert [g["id"] for g in body["groups"]] == [mine.pk] and body["tables"] == {"t": 1}
    for s in ("", None):
        listed = tools.list_duplicates(u, schema=s)
        assert [g["id"] for g in listed["groups"]] == [mine.pk] and listed["truncated"] is False
    one = client(u).get("/api/duplicates", {"schema": HIDDEN})
    two = client(u).get("/api/duplicates", {"schema": MISSING})
    assert (one.status_code, one.content.decode().replace(HIDDEN, "<db>")) == \
        (two.status_code, two.content.decode().replace(MISSING, "<db>"))
    assert _mcp_answer(lambda: tools.list_duplicates(u, schema=HIDDEN), HIDDEN) == \
        _mcp_answer(lambda: tools.list_duplicates(u, schema=MISSING), MISSING)
    admin_view = client(dbs).get("/api/duplicates", {"schema": "*"}).json()
    assert admin_view["tables"] == {"t": 4}


def test_scan_needs_editor_on_that_database(dbs):
    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    r = post(client(u), "/api/duplicates/scan", {"table": "t", "schema": HIDDEN})
    assert r.status_code == 403 and "editor role on 'crm'" in r.json()["error"]
    with pytest.raises(tools.ToolFailure, match="editor role on 'crm'"):
        tools.find_duplicates(u, "t", HIDDEN)
    outsider = grant(make_user(Role.EDITOR), "master", Role.EDITOR)
    assert _mcp_answer(lambda: tools.find_duplicates(outsider, "t", HIDDEN), HIDDEN) == \
        _mcp_answer(lambda: tools.find_duplicates(outsider, "t", MISSING), MISSING)
    a = post(client(outsider), "/api/duplicates/scan", {"table": "t", "schema": HIDDEN})
    b = post(client(outsider), "/api/duplicates/scan", {"table": "t", "schema": MISSING})
    assert (a.status_code, a.content.decode().replace(HIDDEN, "<db>")) == \
        (b.status_code, b.content.decode().replace(MISSING, "<db>"))
    assert not DuplicateGroup.objects.exists()


# ---- scripts --------------------------------------------------------------------------------------

def test_script_autodetected_schema_needs_editor_before_dry_run(dbs, monkeypatch):
    from scripts import service as script_service
    from scripts import validate as validate_mod

    seen = []
    real_snapshot = validate_mod.introspect.snapshot
    monkeypatch.setattr(validate_mod.introspect, "snapshot", lambda s, *a, **k: (seen.append(s), real_snapshot(s, *a, **k))[1])

    dry_runs = []

    def no_dry_run(*a, **k):   # validate's catch-all would swallow an exception: record the call as well
        dry_runs.append(a)
        raise AssertionError("the dry run must not run")

    monkeypatch.setattr(validate_mod.executor, "execute", no_dry_run)
    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    s = make_script(u, validated=False)
    report = script_service.validate(u, s.pk, None)
    assert report["ok"] is False and report["detected_schema"] == HIDDEN
    assert any("editor role on 'crm'" in e["message"] for e in report["errors"]), report["errors"]
    assert seen == [] and dry_runs == [] and Script.objects.get(pk=s.pk).status == Script.Status.UPLOADED
    assert not any("dry run must not run" in e["message"] or "unexpected" in e["message"] for e in report["errors"])
    assert report["dry_run"]["skipped"] is True and [e["kind"] for e in report["errors"]] == ["access"]

    # unreachable: the file's database is treated like one that does not exist here (master is used)
    v = grant(make_user(Role.EDITOR), "master", Role.EDITOR)
    hidden = script_service.validate(v, make_script(v, validated=False).pk, None)
    missing = script_service.validate(v, make_script(v, MISSING, validated=False).pk, None)
    assert HIDDEN not in seen and all(e["kind"] != "access" for e in hidden["errors"])
    assert (hidden["schema"], hidden["schema_source"]) == (missing["schema"], missing["schema_source"]) == \
        ("master", "default")


def test_script_explicit_schema_needs_editor(dbs):
    from scripts import service as script_service

    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    s = make_script(u, "master", validated=False)
    r = post(client(u), f"/api/scripts/{s.pk}/validate", {"schema": HIDDEN})
    assert r.status_code == 403 and "editor role on 'crm'" in r.json()["error"]
    r = post(client(u), f"/api/scripts/{s.pk}/validate?async=1", {"schema": HIDDEN})
    assert r.status_code == 403
    with pytest.raises(tools.ToolFailure, match="editor role on 'crm'"):
        tools.validate_script(u, s.pk, schema=HIDDEN)
    with pytest.raises(tools.ToolFailure, match="editor role on 'crm'"):
        tools.submit_script(u, "a.sql", "CREATE TABLE a (id integer);\n", schema=HIDDEN)
    n = Script.objects.count()
    v = grant(make_user(Role.EDITOR), "master", Role.EDITOR)
    assert _mcp_answer(lambda: tools.submit_script(v, "a.sql", "CREATE TABLE a (id integer);\n", schema=HIDDEN), HIDDEN) \
        == _mcp_answer(lambda: tools.submit_script(v, "a.sql", "CREATE TABLE a (id integer);\n", schema=MISSING), MISSING)
    assert Script.objects.count() == n   # refused before anything is uploaded
    # a script validated for crm cannot be submitted by someone who lost editor there
    done = make_script(u)
    with pytest.raises(access.AccessDenied):
        script_service.submit(u, done.pk)


def test_approver_scripts_list_limited_by_database(dbs, other):
    u = grant(grant(make_user(Role.APPROVER), "master", Role.APPROVER), HIDDEN, Role.EDITOR)
    in_master = make_script(other, "master")
    make_script(other, HIDDEN)            # u is not approver on crm
    make_script(other, validated=False)   # no database known yet: not shown to others
    own = make_script(u, validated=False)
    listed = [s["id"] for s in client(u).get("/api/scripts").json()["scripts"]]
    assert listed == [own.pk, in_master.pk]
    everything = [s["id"] for s in client(dbs).get("/api/scripts").json()["scripts"]]
    assert len(everything) == 4


# ---- loads ----------------------------------------------------------------------------------------

def test_begin_load_needs_editor_and_hides_unreachable(dbs):
    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    with pytest.raises(tools.ToolFailure, match="editor role on 'crm'"):
        tools.begin_load(u, "t", ["code", "name"], HIDDEN)
    v = grant(make_user(Role.EDITOR), "master", Role.EDITOR)
    assert _mcp_answer(lambda: tools.begin_load(v, "t", ["code", "name"], HIDDEN), HIDDEN) == \
        _mcp_answer(lambda: tools.begin_load(v, "t", ["code", "name"], MISSING), MISSING)
    assert not StagedLoad.objects.exists()


# ---- imports --------------------------------------------------------------------------------------

def test_import_start_needs_editor_on_the_database(dbs):
    u = grant(grant(make_user(Role.EDITOR), "master", Role.EDITOR), HIDDEN, Role.VIEWER)
    r = post(client(u), "/api/imports", {"uploadId": 1, "schema": HIDDEN})
    assert r.status_code == 403 and "editor role on 'crm'" in r.json()["error"]
    v = grant(make_user(Role.EDITOR), "master", Role.EDITOR)
    a = post(client(v), "/api/imports", {"uploadId": 1, "schema": HIDDEN})
    b = post(client(v), "/api/imports", {"uploadId": 1, "schema": MISSING})
    assert (a.status_code, a.content.decode().replace(HIDDEN, "<db>")) == \
        (b.status_code, b.content.decode().replace(MISSING, "<db>"))
    assert not ImportRun.objects.exists()


# ---- intelligent query ----------------------------------------------------------------------------

def test_iquery_respects_grants(dbs):
    v = grant(make_user(Role.APPROVER), "master", Role.APPROVER)
    c = client(v)
    for path in ("recall", "suggest", "learn"):
        body = {"comments": ["customers"], "sql": "SELECT 1", "values": []}
        a = post(c, f"/api/intelligent-query/{path}", {**body, "schema": HIDDEN})
        b = post(c, f"/api/intelligent-query/{path}", {**body, "schema": MISSING})
        assert (a.status_code, a.content.decode().replace(HIDDEN, "<db>")) == \
            (b.status_code, b.content.decode().replace(MISSING, "<db>")) and a.status_code == 400, path
    a = c.get("/api/intelligent-query/examples", {"schema": HIDDEN})
    b = c.get("/api/intelligent-query/examples", {"schema": MISSING})
    assert (a.status_code, a.content.decode().replace(HIDDEN, "<db>")) == \
        (b.status_code, b.content.decode().replace(MISSING, "<db>"))
    assert _mcp_answer(lambda: tools.suggest_query(v, HIDDEN, ["customers"]), HIDDEN) == \
        _mcp_answer(lambda: tools.suggest_query(v, MISSING, ["customers"]), MISSING)

    # readable at viewer only: examples (approver) are refused with the role text, the example stays
    grant(v, HIDDEN, Role.VIEWER)
    ex = make_example()
    r = c.get("/api/intelligent-query/examples", {"schema": HIDDEN})
    assert r.status_code == 403 and "approver role on 'crm'" in r.json()["error"]
    r = c.delete(f"/api/intelligent-query/examples/{ex.pk}")
    assert r.status_code == 403 and QueryExample.objects.filter(pk=ex.pk).exists()
    grant(v, HIDDEN, Role.APPROVER)
    assert [e["id"] for e in c.get("/api/intelligent-query/examples", {"schema": HIDDEN}).json()["examples"]] == [ex.pk]
    assert c.delete(f"/api/intelligent-query/examples/{ex.pk}").status_code == 204


# ---- audit ----------------------------------------------------------------------------------------

def _audit_fixture(admin_user):
    """One entry of every database-naming kind for crm (and a master twin), plus entries with no database."""
    crm_group, master_group = make_group(HIDDEN), make_group("master")
    shown, hidden = set(), set()

    def add(bucket, *args, **kw):
        bucket.add(record(admin_user, *args, **kw).pk)

    for s, bucket, g in ((HIDDEN, hidden, crm_group), ("master", shown, master_group)):
        add(bucket, "schema.export", s, {"schema": s, "data": False})
        add(bucket, "table.export_csv", "t", {"table": "t", "schema": s})
        add(bucket, "duplicates.scanned", "t", {"schema": s, "table": "t"})
        add(bucket, "duplicates.dismissed", f"#{g.pk}", {"table": "t"})            # older entries: no schema key
        add(bucket, "load.begin", f"{s}.t", {"load_id": 1, "columns": ["code"]})
        add(bucket, "load.finish", f"{s}.t", {"load_id": 1, "rows": 1, "change_id": 1})
        add(bucket, "backup.created", f"{s}-20261117T000000Z-manual.dump")
        add(bucket, "change.proposal_failed", f"schema {s}", {"kind": "sql", "schema": s, "sql": "x"})
    # change.* entries join to their change; a database op is filed under master but acts on crm
    plain = service.propose(admin_user, [{"op": "create_table", "name": "zz", "columns": [{"name": "a", "type": "text"}]}],
                            source="web", schema=HIDDEN)
    drop = service.propose(admin_user, [{"op": "drop_schema", "schema": HIDDEN, "reason": "gone"}], source="web")
    assert drop.schema == "master"
    for ch in (plain, drop):
        add(hidden, "change.proposed", f"#{ch.pk}", {"source": "web"})
        add(hidden, "change.rejected", f"#{ch.pk}", {"note": ""})
    master_change = service.propose(admin_user, [{"op": "create_table", "name": "zz", "columns": [{"name": "a", "type": "text"}]}],
                                    source="web")
    add(shown, "change.proposed", f"#{master_change.pk}", {"source": "web"})
    add(hidden, "change.rejected", "#999999", {"note": ""})                         # no such change: fail closed
    add(hidden, "duplicates.dismissed", "#999999", {"table": "t"})                  # group gone: fail closed
    add(hidden, "access.changed", "x@x.com", {"before": [], "after": [{"schema": HIDDEN, "role": "viewer"}]})
    # entries with no database: as today
    add(shown, "settings.updated", "a", {"a": 1})
    add(shown, "user.signed_in", "x@x.com")
    add(shown, "search.reindexed", "", {"embedded": 1})
    add(shown, "access.changed", "y@x.com", {"before": [], "after": [{"schema": "*", "role": "viewer"}]})
    config = service.propose(admin_user, [{"op": "set_setting", "key": "search.topK", "value": 7}], source="web")
    assert config.kind == "config"
    add(shown, "change.proposed", f"#{config.pk}", {"source": "web", "kind": "config"})
    return shown, hidden


def test_audit_filters_database_entries(dbs):
    shown, hidden = _audit_fixture(dbs)
    v = grant(grant(make_user(Role.APPROVER), "master", Role.APPROVER), HIDDEN, Role.EDITOR)   # not approver on crm
    seen = {a.pk for a in entries(limit=500, viewer=v)[0]}
    assert shown <= seen and not (hidden & seen)
    web_ids = {e["id"] for e in client(v).get("/api/audit", {"limit": 500}).json()["entries"]}
    assert web_ids == seen
    tool_ids = {e["id"] for e in tools.get_audit_log(v, limit=200)["entries"]}
    assert tool_ids <= seen and not (hidden & tool_ids)
    # an admin, and an approver on crm, see the crm entries; no viewer means unfiltered (internal callers)
    assert hidden <= {a.pk for a in entries(limit=500, viewer=dbs)[0]}
    assert hidden <= {a.pk for a in entries(limit=500)[0]}
    grant(v, HIDDEN, Role.APPROVER)
    crm_seen = {a.pk for a in entries(limit=500, viewer=v)[0]}
    unknowable = set(AuditLog.objects.filter(target="#999999").values_list("pk", flat=True))
    assert (hidden - unknowable) <= crm_seen and not (unknowable & crm_seen)


def test_audit_limit_counts_only_visible_entries(dbs):
    v = grant(make_user(Role.APPROVER), "master", Role.APPROVER)
    AuditLog.objects.all().delete()
    a = record(dbs, "user.signed_in", "a@x.com")
    b = record(dbs, "user.signed_in", "b@x.com")
    for _ in range(5):   # newest entries are all hidden
        record(dbs, "schema.export", HIDDEN, {"schema": HIDDEN})
    items, more = entries(limit=2, viewer=v)
    assert [x.pk for x in items] == [b.pk, a.pk] and more is False
    items, more = entries(limit=1, viewer=v)
    assert [x.pk for x in items] == [b.pk] and more is True
    out = tools.get_audit_log(v, limit=2)
    assert [e["id"] for e in out["entries"]] == [b.pk, a.pk] and out["truncated"] is False


def test_new_entries_name_their_database(dbs):
    """Entries written from now on carry `schema` so the filter does not have to guess."""
    g = make_group("master")
    ed = grant(make_user(Role.EDITOR), access.ALL, Role.EDITOR)
    tools.dismiss_duplicate(ed, g.pk)
    tools.reopen_duplicate(ed, g.pk)
    for action in ("duplicates.dismissed", "duplicates.reopened"):
        assert AuditLog.objects.get(action=action).details["schema"] == "master"
    tools.create_backup(dbs, "master")
    assert AuditLog.objects.filter(action="backup.created").latest("id").details["schema"] == "master"
    SchemaGrant.objects.filter(user=ed).delete()


# ---- fix round 1: a script is for every database its stored report names -------------------------

REVALIDATE_SQL = "CREATE TABLE only_crm (x text);\nCOPY only_crm (x) FROM stdin;\nA\nB\n\\.\n"


@pytest.fixture
def revalidated(dbs, other):
    """`other` validates a script for master (it passes), then checks it again for crm (it fails there)."""
    from scripts import service as script_service

    s = storage.save(other, "r.sql", REVALIDATE_SQL.encode())
    assert script_service.validate(other, s.pk, None, "master")["ok"] is True
    again = script_service.validate(other, s.pk, None, HIDDEN)
    assert again["ok"] is False and again["schema"] == HIDDEN
    s.refresh_from_db()
    assert s.status == Script.Status.VALIDATED and s.report["validated_schema"] == "master"
    return s


@pytest.fixture
def master_approver(dbs):
    return grant(make_user(Role.APPROVER), "master", Role.APPROVER)


def test_failed_revalidation_for_hidden_database_hides_script_web(revalidated, master_approver):
    c = client(master_approver)
    assert revalidated.pk not in [s["id"] for s in c.get("/api/scripts").json()["scripts"]]
    assert _web_answer(c.get(f"/api/scripts/{revalidated.pk}"), revalidated.pk) == \
        _web_answer(c.get(f"/api/scripts/{MISSING_ID}"), MISSING_ID)
    # an approver on both databases still sees it
    grant(master_approver, HIDDEN, Role.APPROVER)
    assert revalidated.pk in [s["id"] for s in c.get("/api/scripts").json()["scripts"]]
    assert c.get(f"/api/scripts/{revalidated.pk}").status_code == 200


def test_failed_revalidation_for_hidden_database_hides_script_mcp(revalidated, master_approver):
    for call in (lambda i: tools.get_script(master_approver, i), lambda i: tools.validate_script(master_approver, i)):
        assert _mcp_answer(lambda: call(revalidated.pk), revalidated.pk) == \
            _mcp_answer(lambda: call(MISSING_ID), MISSING_ID)


def test_check_in_flight_for_hidden_database_hides_script(dbs, other, master_approver, monkeypatch):
    from scripts import jobs
    from scripts import service as script_service

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: None)   # the check never actually runs
    s = storage.save(other, "r.sql", REVALIDATE_SQL.encode())
    assert script_service.validate(other, s.pk, None, "master")["ok"] is True
    assert jobs.start(other, s.pk, None, HIDDEN) == {"state": "running"}
    s.refresh_from_db()
    assert s.report["job"]["schema"] == HIDDEN and s.report["validated_schema"] == "master"
    c = client(master_approver)
    assert s.pk not in [x["id"] for x in c.get("/api/scripts").json()["scripts"]]
    assert _web_answer(c.get(f"/api/scripts/{s.pk}"), s.pk) == _web_answer(c.get(f"/api/scripts/{MISSING_ID}"), MISSING_ID)
    assert _mcp_answer(lambda: tools.get_script(master_approver, s.pk), s.pk) == \
        _mcp_answer(lambda: tools.get_script(master_approver, MISSING_ID), MISSING_ID)


def test_submit_of_someone_elses_script_needs_the_same_reach_as_reading_it(dbs, other):
    from approvals.models import Change
    from scripts import service as script_service

    s = storage.save(other, "r.sql", REVALIDATE_SQL.encode())
    assert script_service.validate(other, s.pk, None, "master")["ok"] is True
    ed = grant(make_user(Role.EDITOR), "master", Role.EDITOR)   # can read master, but it is not their script
    before = Change.objects.count()
    r = post(client(ed), f"/api/scripts/{s.pk}/submit", {})
    assert r.status_code == 403 and "belongs to someone else" in r.json()["error"]
    with pytest.raises(PermissionError):
        script_service.submit(ed, s.pk)
    assert Change.objects.count() == before
    assert Script.objects.get(pk=s.pk).status == Script.Status.VALIDATED
