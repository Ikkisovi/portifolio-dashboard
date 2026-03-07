from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_run_backtest_logs_redacted_docker_command():
    text = _read("run_backtest.ps1")
    assert "function Get-RedactedDockerArgumentString" in text
    assert 'Write-Host "Running: docker $dockerArgString"' not in text
    assert 'Get-RedactedDockerArgumentString -InputArgs $dockerArgs' in text


def test_run_strategy_logs_redacted_docker_command():
    text = _read("run_strategy.ps1")
    assert "function Get-RedactedDockerArgumentString" in text
    assert 'Write-Host "Running: docker $($dockerArgs -join \' \')"' not in text
    assert 'Get-RedactedDockerArgumentString -InputArgs $dockerArgs' in text


def test_launchers_define_sensitive_env_names_for_redaction():
    backtest = _read("run_backtest.ps1")
    strategy = _read("run_strategy.ps1")
    for text in (backtest, strategy):
        assert "ALPACA_API_KEY" in text
        assert "ALPACA_API_SECRET" in text
        assert "ALPACA_PROXY_TOKEN" in text
        assert "IB_PASSWORD" in text
        assert "QC_API_ACCESS_TOKEN" in text
