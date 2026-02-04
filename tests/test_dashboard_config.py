from dashboard import config


def test_example_data_settings_present():
    assert "MU" in config.EXAMPLE_TICKERS
    assert config.EXAMPLE_START_DATE == "2025-10-01"
    assert config.EXAMPLE_BASE_CAPITAL == 100000


def test_light_theme_colors():
    assert config.COLORS["background"] == "#f7f7f5"
