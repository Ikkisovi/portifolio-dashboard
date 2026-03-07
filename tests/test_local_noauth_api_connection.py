from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_api_connection_supports_local_noauth_bypass():
    text = _read("Lean-full/Api/ApiConnection.cs")
    assert 'Environment.GetEnvironmentVariable("LEAN_LOCAL_NOAUTH")' in text
    assert "ApiConnection.Connected(): skipped because LEAN_LOCAL_NOAUTH is enabled." in text


def test_launchers_mount_api_override_for_local_noauth():
    for path in ("run_strategy.ps1", "run_backtest.ps1"):
        text = _read(path)
        assert "QuantConnect.Api.dll" in text
        assert "QuantConnect.Api.deps.json" in text
