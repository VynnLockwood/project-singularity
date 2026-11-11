from config.siconfig import get_config
from settings_app.tunables import TUNABLES


def test_defaults():
    cfg = get_config()
    assert cfg["limits"]["maxLoadChunkBytes"] == 262144
    assert cfg["limits"]["maxLoadChunkRows"] == 5000
    assert cfg["limits"]["maxLoadBytes"] == 104857600
    assert cfg["loads"]["ttlHours"] == 24
    assert cfg["loads"]["maxOpenPerUser"] == 5


def test_tunables_have_ranges():
    want = {"limits.maxLoadChunkBytes": (4096, 2097152), "limits.maxLoadChunkRows": (1, 20000),
            "limits.maxLoadBytes": (1048576, 2147483648), "loads.ttlHours": (1, 168),
            "loads.maxOpenPerUser": (1, 50)}
    for key, (lo, hi) in want.items():
        label, help_, kind, mn, mx = TUNABLES[key]
        assert kind == "int" and (mn, mx) == (lo, hi) and label and help_
