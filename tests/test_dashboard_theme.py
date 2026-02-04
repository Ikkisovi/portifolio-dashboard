from dashboard.frontend import components


def test_theme_css_includes_font():
    css = components.get_global_styles()
    assert "Space Grotesk" in css


def test_metrics_bar_markup():
    html = components.build_metrics_bar_html(
        equity="$100",
        fees="$0",
        holdings="$50",
        net_profit="$10",
        psr="60%",
        unrealized="$5",
        cash="$50",
    )
    assert "metrics-card" in html
