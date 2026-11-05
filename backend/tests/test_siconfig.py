from config import siconfig


def test_defaults_and_overrides(tmp_path, monkeypatch):
    f = tmp_path / "s.yaml"
    f.write_text(
        "auth:\n  allowedDomains: [Example.COM]\n  seedAdmins: [Boss@Example.com]\n"
        "limits:\n  maxQueryRows: 10\n"
    )
    monkeypatch.setenv("SINGULARITY_CONFIG", str(f))
    siconfig.get_config.cache_clear()
    try:
        cfg = siconfig.get_config()
        assert cfg["auth"]["allowedDomains"] == ["example.com"]  # lower-cased
        assert cfg["auth"]["seedAdmins"] == ["boss@example.com"]
        assert cfg["auth"]["defaultRole"] == "viewer"  # default kept
        assert cfg["limits"]["maxQueryRows"] == 10  # overridden
        assert cfg["limits"]["queryTimeoutMs"] == 5000  # default kept
    finally:
        siconfig.get_config.cache_clear()


def test_missing_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULARITY_CONFIG", str(tmp_path / "nope.yaml"))
    siconfig.get_config.cache_clear()
    try:
        assert siconfig.get_config()["auth"]["allowedDomains"] == []
    finally:
        siconfig.get_config.cache_clear()
