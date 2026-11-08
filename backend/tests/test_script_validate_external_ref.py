"""Task 3: a ref whose parent already exists resolves against the live table (external ref).

The load-bearing assertion is the orphan count: every row still comes from the script's own
VALUES one by one, so a district_code that master.district does not have is reported instead of
being loaded blind. Both tests below would also pass on an empty parent table if rows stopped
being inspected, which is why the bad-code test insists on exactly one orphan.
"""
import pytest

from accounts.models import Role, User
from gate import check
from schema import executor, introspect
from scripts import storage
from scripts.validate import validate

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master")]

ME = "ed@x.io"

EXT = {"tables": {"subdistrict_src": {
    "target": "subdistrict",
    "columns": {"code": "code", "name": "name", "postcode": "postcode"},
    "refs": [{"column": "district_id", "parent": "master.district", "parent_key": "code",
              "source_column": "district_code", "external": True}]}}}

SCRIPT = ("INSERT INTO master.subdistrict_src (code, name, postcode, district_code) "
          "SELECT v.code, v.name, v.postcode, v.district_code "
          "FROM (VALUES {}) AS v(code, name, postcode, district_code) "
          "JOIN master.district d ON d.code = v.district_code;")


def script(*codes: str) -> bytes:
    rows = ", ".join(f"('1001{i:02d}', 'Bang Rak {i}', '10500', '{c}')" for i, c in enumerate(codes, 1))
    return SCRIPT.format(rows).encode("utf-8")


def tbl(report, name="subdistrict"):
    return next(t for t in report["tables"] if t["name"] == name)


@pytest.fixture
def owner():
    return User.objects.create(username=ME, email=ME, role=Role.EDITOR)


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    settings.SCRIPT_DIR = str(tmp_path / "scripts")


@pytest.fixture
def live_parent():
    """master.district holding two codes (the external parent) plus the subdistrict target."""
    def apply(ops):
        state = introspect.snapshot()
        res = check.run(ops, state, [], actor=ME)
        assert res.ok, (res.errors, res.violations)
        executor.execute(res.operations, state, ME, rules=[])

    apply([
        {"op": "create_table", "name": "district",
         "columns": [{"name": "code", "type": "text", "primary_key": True, "unique": True}]},
        {"op": "insert_rows", "table": "district", "rows": [{"code": "1001"}, {"code": "1002"}]},
        {"op": "create_table", "name": "subdistrict",
         "columns": [{"name": "code", "type": "text"}, {"name": "name", "type": "text"},
                     {"name": "postcode", "type": "text"}, {"name": "district_id", "type": "text"}]},
    ])


def test_external_ref_flags_a_code_the_live_parent_does_not_have(owner, live_parent):
    report = validate(storage.save(owner, "ext.sql", script("1001", "9999")), EXT)
    entry = tbl(report)
    assert entry["rows"] == 2
    assert entry["orphans"] == 1, entry                     # the bad row was inspected, not skipped
    (sample,) = entry["sample_orphans"]
    assert sample["value"] == "9999" and sample["parent"] == "district.code"
    assert report["errors"] == [] and not report["ok"]


def test_external_ref_resolves_a_code_the_live_parent_has(owner, live_parent):
    report = validate(storage.save(owner, "ext.sql", script("1001", "1002")), EXT)
    entry = tbl(report)
    assert entry["rows"] == 2
    assert entry["orphans"] == 0, entry                     # the live rows of master.district were read
    assert report["errors"] == [] and report["gate"]["errors"] == []
