from dashboard.frontend import charts


def test_plotly_template_is_light():
    assert charts.get_plotly_template_name() == "plotly_white"
