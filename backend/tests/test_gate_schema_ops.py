import pytest

from gate import check, operations
from gate.defaults import DEFAULT_RULES
from settings_app import tunables
from config.siconfig import DEFAULTS

RULES = [{**r, "enabled": True} for r in DEFAULT_RULES]
MANAGED = {"master": 3, "staging": 0, "full": 2}


def run(ops, managed=MANAGED, max_schemas=20, skip_rules=False):
    return check.run(ops, {}, RULES, managed_schemas=managed, max_schemas=max_schemas, skip_rules=skip_rules)


def test_parse_accepts_three_ops():
    for op in ({"op": "create_schema", "name": "sales", "comment": "x"},
               {"op": "rename_schema", "schema": "staging", "new_name": "stage2"},
               {"op": "drop_schema", "schema": "staging", "reason": "unused"}):
        assert operations.parse_ops([op]) == [op]
        assert operations.kind_of([op]) == "schema"
        assert operations.affected_tables([op]) == []
    assert "drop_schema" in operations.DESTRUCTIVE_OPS
    assert "create_schema" not in operations.DESTRUCTIVE_OPS


@pytest.mark.parametrize("name", ["Public", "public", "singularity", "information_schema", "pg_x", "pg_catalog",
                                  "1abc", "a-b", "", "A", "x" * 64, "sales ", "sales\n", None, 5])
def test_bad_names_refused(name):
    with pytest.raises(operations.OpError) as e:
        operations.parse_ops([{"op": "create_schema", "name": name}])
    assert "schema name" in e.value.errors[0]


def test_valid_schema_name():
    assert operations.valid_schema_name("a" * 63) is None
    assert operations.valid_schema_name("master") is None  # existence is a state check
    assert operations.valid_schema_name("pg_x")


def test_drop_needs_reason():
    with pytest.raises(operations.OpError):
        operations.parse_ops([{"op": "drop_schema", "schema": "staging"}])


def test_no_mixing():
    with pytest.raises(operations.OpError) as e:
        operations.parse_ops([{"op": "create_schema", "name": "sales"},
                              {"op": "create_table", "name": "t", "columns": [{"name": "a", "type": "text"}]}])
    assert "alone" in " ".join(e.value.errors)
    with pytest.raises(operations.OpError):
        operations.parse_ops([{"op": "create_schema", "name": "a1"}, {"op": "create_schema", "name": "a2"}])


def test_create_ok_and_limits():
    assert run([{"op": "create_schema", "name": "sales"}]).ok
    r = run([{"op": "create_schema", "name": "sales"}], max_schemas=3)
    assert not r.ok and "maxSchemas" in r.errors[0]
    r = run([{"op": "create_schema", "name": "full"}])
    assert not r.ok and "already exists" in r.errors[0]


def test_rename():
    assert run([{"op": "rename_schema", "schema": "staging", "new_name": "s2"}]).ok
    for ops, text in (
        ({"op": "rename_schema", "schema": "master", "new_name": "m2"}, "master"),
        ({"op": "rename_schema", "schema": "nope", "new_name": "m2"}, "does not exist"),
        ({"op": "rename_schema", "schema": "staging", "new_name": "full"}, "already exists"),
    ):
        r = run([ops])
        assert not r.ok and text in " ".join(r.errors)


def test_drop():
    assert run([{"op": "drop_schema", "schema": "staging", "reason": "unused"}]).ok
    r = run([{"op": "drop_schema", "schema": "full", "reason": "unused"}])
    assert not r.ok and "2 table" in r.errors[0]
    r = run([{"op": "drop_schema", "schema": "master", "reason": "unused"}])
    assert not r.ok and "master" in r.errors[0]
    r = run([{"op": "drop_schema", "schema": "nope", "reason": "unused"}])
    assert not r.ok


def test_rules_run_on_database_ops():
    rules = RULES + [{"name": "no-drop-schema", "type": "forbidden", "severity": "block", "enabled": True,
                      "params": {"ops": ["drop_schema"]}, "description": "no"}]
    r = check.run([{"op": "drop_schema", "schema": "staging", "reason": "unused"}], {}, rules,
                  managed_schemas=MANAGED)
    assert not r.ok and r.violations
    assert check.run([{"op": "create_schema", "name": "sales"}], {}, RULES, managed_schemas=MANAGED).ok


def test_tunable():
    assert DEFAULTS["limits"]["maxSchemas"] == 20
    spec = tunables.TUNABLES["limits.maxSchemas"]
    assert spec[2:] == ("int", 1, 200)
