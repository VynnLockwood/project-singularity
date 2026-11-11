"""A 250 MB, 192,000-row, 80-table dump validates within a memory bound (slow)."""
import time
import tracemalloc

import pytest

from accounts.models import Role, User
from scripts import storage
from scripts.validate import validate
from tests import bigdump

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("master"), pytest.mark.slow]

# Python-heap peak (tracemalloc) of validate on a 250 MB dump, in MiB: measured 637, limit = +25%.
# storage.save (legacy single-request upload) peaks higher by design; the chunked path replaces it (Task 3).
MEM_LIMIT_MB = 800


@pytest.fixture(autouse=True)
def script_dir(settings, tmp_path):
    from config.siconfig import invalidate_overrides
    settings.SCRIPT_DIR = str(tmp_path / "scripts")
    invalidate_overrides()
    yield
    invalidate_overrides()


def test_250mb_dump_validates_within_memory_bound(tmp_path, monkeypatch):
    from config.siconfig import get_config
    cfg = get_config()
    big = {**cfg, "limits": {**cfg["limits"], "maxScriptBytes": 536870912}}   # until the tunable range is raised
    monkeypatch.setattr(storage, "get_config", lambda: big)
    info = bigdump.write_dump(tmp_path / "dump.sql")
    assert info["rows"] == 192_000 and info["bytes"] > 200 * 2**20
    ed = User.objects.create(username="ed@x.io", email="ed@x.io", role=Role.EDITOR)
    started = time.monotonic()
    tracemalloc.start()
    data = (tmp_path / "dump.sql").read_bytes()
    script = storage.save(ed, "dump.sql", data)
    del data
    save_peak = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.reset_peak()
    report = validate(script, None, "master")
    peak = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.stop()
    print(f"\nSCALE save_peak_mib={save_peak:.0f} seconds={time.monotonic() - started:.0f} peak_mib={peak:.0f} bytes={info['bytes']}")
    assert report["ok"], (report["errors"][:3], report["gate"], report["dry_run"])
    assert len(report["tables"]) == 80 and sum(t["rows"] for t in report["tables"]) == 192_000
    if MEM_LIMIT_MB is not None:
        assert peak < MEM_LIMIT_MB
