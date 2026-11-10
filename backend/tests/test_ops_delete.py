"""delete_rows (Plan B, task 5)."""
import importlib

import pytest
from django.apps import apps

from accounts.models import Role, User
from approvals import service
from gate import operations, sqlgen
from schema import executor, introspect
from settings_app.models import Rule

from .ops_helpers import RULES, apply, col, gate, make_table, rows

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

DEL = {"op": "delete_rows", "table": "t", "where": {"t_id": [1, 2]}, "reason": "cleanup of test rows"}


def errors_of(op):
    try:
        operations.parse_ops([op])
    except operations.OpError as e:
        return e.errors
    return []


def test_shape():
    assert operations.parse_ops([DEL])
    assert errors_of({**DEL, "where": {}}) and errors_of({**DEL, "where": {"a": None}})
    assert errors_of({k: v for k, v in DEL.items() if k != "reason"}) and errors_of({**DEL, "x": 1})
    assert "delete_rows" in operations.DESTRUCTIVE_OPS and "delete_rows" in operations.DATA_OPS
    assert operations.kind_of([DEL]) == "data"


def test_sql_and_diff():
    make_table(rows=[{"a": "x"}] * 3)
    res = gate([DEL])
    assert res.ok and res.sql == ['DELETE FROM "master"."t" WHERE "t_id" IN (1, 2);']
    assert res.diff == ["- delete rows from t where {'t_id': [1, 2]}  (reason: cleanup of test rows)"]
    assert gate([{**DEL, "where": {"nope": 1}}]).errors and gate([{**DEL, "table": "nope"}]).errors


def test_delete_removes_rows_and_reverse_inserts_them_back():
    make_table(rows=[{"a": "x"}, {"a": "y"}, {"a": "z"}])
    before = rows("SELECT t_id, a FROM t ORDER BY 1")
    res = apply([DEL])
    assert rows("SELECT t_id FROM t") == [[3]]
    assert len(res.reverse_ops) == 1 and res.reverse_ops[0]["op"] == "insert_rows"
    assert {r["t_id"] for r in res.reverse_ops[0]["rows"]} == {1, 2}
    apply(res.reverse_ops)
    assert rows("SELECT t_id, a FROM t ORDER BY 1") == before


def test_delete_matching_nothing_changes_nothing():
    make_table(rows=[{"a": "x"}])
    assert apply([{**DEL, "where": {"t_id": 99}}]).reverse_ops is None      # nothing to put back
    assert rows("SELECT count(*) FROM t") == [[1]]


def test_delete_over_the_cap_is_allowed_but_irreversible():
    make_table(rows=[{"a": "x"}] * 5)
    res = apply([{**DEL, "where": {"a": "x"}}], max_rows=2)
    assert rows("SELECT count(*) FROM t") == [[0]] and res.reverse_ops is None


def test_delete_blocked_by_restricting_foreign_key_names_the_constraint():
    make_table("p", rows=[{"a": "x"}])
    apply([{"op": "create_table", "name": "c", "columns": [
        col("c_id", "integer", primary_key=True, identity=True),
        col("p_id", "integer", references={"table": "p", "on_delete": "restrict"})]}])
    apply([{"op": "insert_rows", "table": "c", "rows": [{"p_id": 1}]}])
    with pytest.raises(executor.ApplyError, match="fk_c_p_id"):
        apply([{"op": "delete_rows", "table": "p", "where": {"p_id": 1}, "reason": "remove parent"}])
    assert rows("SELECT count(*) FROM p") == [[1]]


def test_care_with_deletes_rule_warns():
    make_table(rows=[{"a": "x"}] * 2)
    res = gate([DEL], skip_rules=False)
    warns = [v for v in res.violations if v["rule"] == "careWithDeletes"]
    assert warns and warns[0]["severity"] == "warn" and res.ok


def test_propose_adds_the_row_count_warning():
    make_table(rows=[{"a": "x"}] * 3)
    editor = User.objects.create(username="e@x.com", email="e@x.com", role=Role.EDITOR)
    ch = service.propose(editor, [DEL], source="web")
    assert ch.status == "pending"
    counts = [v for v in ch.check_result["violations"] if v["rule"] == "deleteRowCount"]
    assert len(counts) == 1 and "2 row(s)" in counts[0]["message"] and counts[0]["severity"] == "warn"


def test_migration_adds_the_rule_to_existing_installs():
    mig = importlib.import_module("settings_app.migrations.0003_care_with_deletes")
    Rule.objects.filter(name="careWithDeletes").delete()
    assert Rule.objects.exists()
    mig.add_rule(apps, None)
    assert Rule.objects.filter(name="careWithDeletes").count() == 1
    mig.add_rule(apps, None)
    assert Rule.objects.filter(name="careWithDeletes").count() == 1
    Rule.objects.all().delete()
    mig.add_rule(apps, None)                     # fresh install: the seeding adds every default instead
    assert not Rule.objects.exists()
