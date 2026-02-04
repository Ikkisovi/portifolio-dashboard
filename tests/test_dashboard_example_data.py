from dashboard.backend import data_loader


def test_example_equity_from_real_data():
    equity = data_loader.load_example_equity()
    assert isinstance(equity, list)
    assert len(equity) > 10
    sample = equity[0]
    assert "datetime" in sample and "close" in sample


def test_example_positions_real_tickers():
    positions = data_loader.load_example_positions()
    symbols = {p["symbol"] for p in positions}
    assert {"MU", "SNDK", "CDE", "RKLB"}.issubset(symbols)


def test_example_account_has_runtime_stats():
    account = data_loader.load_example_account()
    stats = account.get("runtimeStatistics", {})
    assert "Equity" in stats and "Holdings" in stats
