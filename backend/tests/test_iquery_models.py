import pytest

from config.siconfig import get_config

pytestmark = pytest.mark.django_db


def test_defaults():
    assert get_config()["intelligentQuery"] == {
        "model": "", "maxTables": 8, "examples": 3, "memoryHit": 0.95, "ratePerMinute": 20, "valueLookupMs": 800}


def test_example_roundtrip():
    from iquery.models import QueryExample
    ex = QueryExample.objects.create(schema="master", comments="a", sql="select 1", sql_norm="select 1",
                                     values=[], embedding=[0.1] * 384)
    ex.refresh_from_db()
    assert ex.status == "candidate" and ex.confirmations == [] and ex.use_count == 1
