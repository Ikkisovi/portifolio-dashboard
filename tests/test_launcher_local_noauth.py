from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_run_strategy_supports_local_noauth_flag():
    text = _read("run_strategy.ps1")
    assert "LEAN_LOCAL_NOAUTH" in text
    assert "$leanLocalNoAuth" in text
    assert "if ($leanLocalNoAuth)" in text


def test_run_strategy_skips_qc_prompts_in_local_noauth_mode():
    text = _read("run_strategy.ps1")
    assert 'Read-Host "QC job-user-id"' in text
    assert 'Read-Host "QC api-access-token"' in text
    assert 'Read-Host "QC job-organization-id"' in text
    assert 'Write-Host "QC auth skipped: LEAN_LOCAL_NOAUTH=1"' in text


def test_run_strategy_normalizes_optional_qc_fields_in_local_noauth_mode():
    text = _read("run_strategy.ps1")
    assert "function Normalize-OptionalIntConfig" in text
    assert "function Normalize-OptionalStringConfig" in text
    assert "function Remove-JsonArrayValue" in text
    assert "function Remove-InteractiveBrokersDataQueueHandler" in text
    assert 'Normalize-OptionalIntConfig -Object $configJson -Name "job-user-id"' in text
    assert 'Normalize-OptionalIntConfig -Object $configJson -Name "project-id"' in text
    assert 'Normalize-OptionalStringConfig -Object $configJson -Name "api-access-token"' in text
    assert 'Normalize-OptionalStringConfig -Object $configJson -Name "job-organization-id"' in text
    assert 'if ($selectedBrokerage -eq "PaperBrokerage")' in text
    assert 'Remove-InteractiveBrokersDataQueueHandler -ConfigObject $configJson' in text


def test_run_strategy_keeps_stock_api_handler():
    text = _read("run_strategy.ps1")
    assert 'Set-JsonValue -Object $configJson -Name "api-handler"' not in text
    assert 'Set-JsonValue -Object $configJson -Name "job-queue-handler"' not in text
    assert 'Set-JsonValue -Object $configJson -Name "messaging-handler"' not in text


def test_run_backtest_supports_local_noauth_flag():
    text = _read("run_backtest.ps1")
    assert "LEAN_LOCAL_NOAUTH" in text
    assert "$leanLocalNoAuth" in text
    assert "if ($leanLocalNoAuth)" in text


def test_run_backtest_normalizes_blank_qc_fields_for_local_noauth():
    text = _read("run_backtest.ps1")
    assert "function Normalize-OptionalStringConfig" in text
    assert 'Normalize-OptionalIntConfig -Object $configJson -Name "job-user-id"' in text
    assert 'Normalize-OptionalIntConfig -Object $configJson -Name "project-id"' in text
    assert 'Normalize-OptionalStringConfig -Object $configJson -Name "api-access-token"' in text
    assert 'Normalize-OptionalStringConfig -Object $configJson -Name "job-organization-id"' in text


def test_run_backtest_does_not_introduce_qc_prompt_requirement():
    text = _read("run_backtest.ps1")
    assert 'Read-Host "QC job-user-id"' not in text
    assert 'Read-Host "QC api-access-token"' not in text
    assert 'Read-Host "QC job-organization-id"' not in text
    assert 'Write-Host "QC auth skipped: LEAN_LOCAL_NOAUTH=1"' in text


def test_run_strategy_passes_local_noauth_to_container():
    text = _read("run_strategy.ps1")
    assert '"-e", "LEAN_LOCAL_NOAUTH=$env:LEAN_LOCAL_NOAUTH"' in text


def test_run_backtest_passes_local_noauth_to_container():
    text = _read("run_backtest.ps1")
    assert '"-e", "LEAN_LOCAL_NOAUTH=$env:LEAN_LOCAL_NOAUTH"' in text


def test_launcher_override_scan_includes_alpaca_brokerage_dependencies():
    strategy = _read("run_strategy.ps1")
    backtest = _read("run_backtest.ps1")
    for text in (strategy, backtest):
        assert 'Alpaca.Markets.dll' in text
        assert 'QuantConnect.Brokerages.Alpaca.dll' in text
        assert 'QuantConnect.Brokerages.Alpaca.deps.json' in text


def test_s_paper_mom10_paper_config_uses_only_alpaca_proxy_data_queue():
    text = _read("S_paper_mom10/launcher_config.json")
    assert '"QuantConnect.AlpacaProxy.AlpacaProxyDataQueueHandler"' in text
    assert '"QuantConnect.Brokerages.InteractiveBrokers.InteractiveBrokersBrokerage"' not in text
