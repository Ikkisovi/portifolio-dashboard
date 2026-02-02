"""
Chart rendering functions for LEAN Live Trading Dashboard
Handles Plotly chart creation and rendering.
"""

import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

from .. import config

CHART_HEIGHTS = config.CHART_HEIGHTS
COLORS = config.COLORS


def _debug_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    return


def render_equity_chart(equity_df: pd.DataFrame) -> None:
    # region agent log
    _debug_log(
        "H3",
        "charts.py:50",
        "Render equity chart entry",
        {
            "is_none": equity_df is None,
            "rows": 0 if equity_df is None else len(equity_df),
            "cols": [] if equity_df is None else list(equity_df.columns),
        },
    )
    # endregion
    if equity_df is None or (hasattr(equity_df, "empty") and equity_df.empty):
        # Force-load example equity data directly if available
        example_path = Path(config.EXAMPLE_DATA_DIR) / "equity.json"
        if example_path.exists():
            try:
                equity_df = pd.DataFrame(json.loads(example_path.read_text()))
            except Exception:
                equity_df = pd.DataFrame()
        else:
            equity_df = pd.DataFrame()

    if equity_df is None or equity_df.empty:
        st.info("Waiting for equity data...")
        return

    equity_df = equity_df.copy()
    equity_df["datetime"] = pd.to_datetime(equity_df["datetime"], errors="coerce")
    if "close" not in equity_df.columns and "equity" in equity_df.columns:
        equity_df["close"] = equity_df["equity"]
    if equity_df["close"].dtype == object:
        equity_df["close"] = equity_df["close"].astype(str).str.replace("$", "", regex=False).str.replace(",", "")
    equity_df["close"] = pd.to_numeric(equity_df["close"], errors="coerce")
    equity_df = equity_df.dropna(subset=["datetime", "close"]).sort_values("datetime")

    # region agent log
    _debug_log(
        "H3",
        "charts.py:75",
        "Equity data after sanitize",
        {
            "rows": len(equity_df),
            "datetime_nulls": int(equity_df["datetime"].isna().sum()) if "datetime" in equity_df.columns else None,
            "close_nulls": int(equity_df["close"].isna().sum()) if "close" in equity_df.columns else None,
        },
    )
    # endregion

    if equity_df.empty:
        st.info("No valid equity data")
        return

    env_plotly = os.getenv("DASHBOARD_USE_PLOTLY")
    use_plotly = env_plotly == "1"
    # region agent log
    _debug_log(
        "H7",
        "charts.py:96",
        "Equity chart renderer selected",
        {"use_plotly": use_plotly, "env_plotly": env_plotly},
    )
    # endregion

    if use_plotly:
        base_close = float(equity_df["close"].iloc[0]) if not equity_df.empty else None
        norm_df = equity_df.copy()
        if base_close and base_close != 0:
            norm_df["open"] = (norm_df["open"] / base_close) * 100.0
            norm_df["high"] = (norm_df["high"] / base_close) * 100.0
            norm_df["low"] = (norm_df["low"] / base_close) * 100.0
            norm_df["close"] = (norm_df["close"] / base_close) * 100.0
        norm_df[["open", "high", "low", "close"]] = norm_df[["open", "high", "low", "close"]].astype(float)
        y_min = float(norm_df["low"].min())
        y_max = float(norm_df["high"].max())
        pad = max((y_max - y_min) * 0.05, 0.5)

        # region agent log
        _debug_log(
            "H10",
            "charts.py:115",
            "Plotly normalized range",
            {
                "base_close": base_close,
                "y_min": y_min,
                "y_max": y_max,
                "pad": pad,
            },
        )
        # endregion

        # region agent log
        _debug_log(
            "H11",
            "charts.py:128",
            "Plotly data snapshot",
            {
                "rows": len(norm_df),
                "x_first": str(norm_df["datetime"].iloc[0]) if not norm_df.empty else None,
                "x_last": str(norm_df["datetime"].iloc[-1]) if not norm_df.empty else None,
                "open_nulls": int(norm_df["open"].isna().sum()),
                "high_nulls": int(norm_df["high"].isna().sum()),
                "low_nulls": int(norm_df["low"].isna().sum()),
                "close_nulls": int(norm_df["close"].isna().sum()),
                "open_min": float(norm_df["open"].min()) if not norm_df.empty else None,
                "open_max": float(norm_df["open"].max()) if not norm_df.empty else None,
            },
        )
        # endregion

        x_vals = norm_df["datetime"].dt.to_pydatetime()
        # region agent log
        _debug_log(
            "H15",
            "charts.py:139",
            "Plotly x types",
            {
                "x_type": type(x_vals[0]).__name__ if len(x_vals) else None,
                "x_sample": str(x_vals[0]) if len(x_vals) else None,
            },
        )
        # endregion

        # region agent log
        _debug_log(
            "H5",
            "charts.py:163",
            "Equity axis range computed",
            {
                "y_min": y_min,
                "y_max": y_max,
                "pad": pad,
                "first_dt": str(equity_df["datetime"].iloc[0]) if not equity_df.empty else None,
                "last_dt": str(equity_df["datetime"].iloc[-1]) if not equity_df.empty else None,
            },
        )
        # endregion

        env_candle = os.getenv("DASHBOARD_PLOTLY_CANDLE")
        use_candles = (env_candle if env_candle is not None else "1") == "1"
        # region agent log
        _debug_log(
            "H14",
            "charts.py:142",
            "Plotly price mode",
            {"use_candles": use_candles, "env_candle": env_candle},
        )
        # endregion

        # OHLCV: use provided volume if available, else synthesize for visualization
        if "volume" in norm_df.columns:
            vol = pd.to_numeric(norm_df["volume"], errors="coerce").fillna(0).astype(float)
            vol_source = "data"
        else:
            vol = (norm_df["close"].pct_change().abs().fillna(0) * 1_000_000 + 50_000).astype(float)
            vol_source = "synthetic"

        # region agent log
        _debug_log(
            "H17",
            "charts.py:155",
            "Volume series prepared",
            {
                "vol_source": vol_source,
                "vol_min": float(vol.min()) if len(vol) else None,
                "vol_max": float(vol.max()) if len(vol) else None,
            },
        )
        # endregion

        show_line = os.getenv("DASHBOARD_PLOTLY_LINE", "1") == "1"
        # region agent log
        _debug_log(
            "H19",
            "charts.py:150",
            "Plotly line overlay",
            {"show_line": show_line},
        )
        # endregion

        if use_candles:
            fig_price = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.03,
                row_heights=[0.75, 0.25],
            )
            fig_price.add_trace(go.Candlestick(
                x=x_vals,
                open=norm_df["open"],
                high=norm_df["high"],
                low=norm_df["low"],
                close=norm_df["close"],
                name="Equity",
                increasing=dict(line=dict(color="#00ff88", width=3), fillcolor="#00ff88"),
                decreasing=dict(line=dict(color="#ff3355", width=3), fillcolor="#ff3355"),
                opacity=1.0,
                whiskerwidth=0.8,
            ), row=1, col=1)
            # region agent log
            body_sizes = (norm_df["close"] - norm_df["open"]).abs()
            _debug_log(
                "H20",
                "charts.py:205",
                "Candlestick body sizes",
                {
                    "body_min": float(body_sizes.min()) if len(body_sizes) else None,
                    "body_max": float(body_sizes.max()) if len(body_sizes) else None,
                    "body_mean": float(body_sizes.mean()) if len(body_sizes) else None,
                },
            )
            # endregion
            if show_line:
                fig_price.add_trace(go.Scatter(
                    x=x_vals,
                    y=norm_df["close"],
                    mode="lines",
                    name="Equity Line",
                    line=dict(color="#ffd54a", width=2),
                    showlegend=False,
                ), row=1, col=1)
            fig_price.add_trace(go.Bar(
                x=x_vals,
                y=vol.values,
                name="Volume",
                marker_color="#777777",
                opacity=0.6,
            ), row=2, col=1)

            fig_price.update_layout(
                template="plotly_dark",
                height=CHART_HEIGHTS.get("equity", 500),
                margin=dict(l=10, r=10, t=10, b=10),
                showlegend=False,
                xaxis=dict(type="date"),
                bargap=0.02,
            )
            fig_price.update_yaxes(
                title_text="Equity (Indexed, base=100)",
                tickformat=",.2f",
                range=[y_min - pad, y_max + pad],
                zeroline=True,
                zerolinecolor="#444",
                row=1, col=1,
            )
            fig_price.update_yaxes(
                title_text="Volume",
                showgrid=False,
                row=2, col=1,
            )
            fig_price.update_xaxes(rangeslider_visible=False, row=1, col=1)
        else:
            fig_price = go.Figure()
            fig_price.add_trace(go.Scatter(
                x=x_vals,
                y=norm_df["close"],
                mode="lines+markers",
                name="Equity Line",
                line=dict(color="#ffd54a", width=2),
                marker=dict(size=6, color="#ffd54a"),
                showlegend=False,
            ))
            fig_price.update_layout(
                template="plotly_dark",
                height=CHART_HEIGHTS.get("equity", 500),
                margin=dict(l=10, r=10, t=10, b=10),
                showlegend=False,
                xaxis=dict(type="date"),
            )
            fig_price.update_yaxes(
                title_text="Equity (Indexed, base=100)",
                tickformat=",.2f",
                autorange=True,
                zeroline=True,
                zerolinecolor="#444",
            )
            fig_price.update_xaxes(rangeslider_visible=False)

        # region agent log
        _debug_log(
            "H18",
            "charts.py:222",
            "Plotly price figure summary",
            {
                "built_subplots": bool(use_candles),
                "trace_types": [t.type for t in fig_price.data],
                "has_candlestick": any(getattr(t, "type", None) == "candlestick" for t in fig_price.data),
                "has_scatter": any(getattr(t, "type", None) == "scatter" for t in fig_price.data),
                "has_volume_bar": any(getattr(t, "type", None) == "bar" for t in fig_price.data),
                "ohlc_first": {
                    "open": float(norm_df["open"].iloc[0]) if len(norm_df) else None,
                    "high": float(norm_df["high"].iloc[0]) if len(norm_df) else None,
                    "low": float(norm_df["low"].iloc[0]) if len(norm_df) else None,
                    "close": float(norm_df["close"].iloc[0]) if len(norm_df) else None,
                },
            },
        )
        # endregion

        # region agent log
        _debug_log(
            "H6",
            "charts.py:168",
            "Plotly price chart ready",
            {
                "trace_count": len(fig_price.data),
                "layout_height": fig_price.layout.height,
                "template": fig_price.layout.template.__class__.__name__ if fig_price.layout.template else None,
                "trace_types": [t.type for t in fig_price.data],
            },
        )
        # endregion
        # region agent log
        _debug_log(
            "H16",
            "charts.py:176",
            "Plotly price data sample",
            {
                "y_first": float(norm_df["close"].iloc[0]) if not norm_df.empty else None,
                "y_last": float(norm_df["close"].iloc[-1]) if not norm_df.empty else None,
            },
        )
        # endregion

        env_html = os.getenv("DASHBOARD_PLOTLY_HTML")
        use_html = (env_html if env_html is not None else ("1" if use_candles else "0")) == "1"
        # region agent log
        _debug_log(
            "H21",
            "charts.py:180",
            "Plotly render mode",
            {"use_html": use_html, "env_html": env_html},
        )
        # endregion
        if use_html:
            html = fig_price.to_html(include_plotlyjs="cdn")
            components.html(html, height=CHART_HEIGHTS.get("equity", 500), scrolling=False)
        else:
            # region agent log
            _debug_log(
                "H12",
                "charts.py:188",
                "Plotly render start",
                {"use_container_width": True},
            )
            # endregion
            st.plotly_chart(fig_price, use_container_width=True, key="equity_price_plotly")
            # region agent log
            _debug_log(
                "H12",
                "charts.py:192",
                "Plotly render end",
                {"rendered": True},
            )
            # endregion

        show_returns = os.getenv("DASHBOARD_PLOTLY_RETURNS", "0") == "1"
        if show_returns:
            point_return = norm_df["close"].pct_change() * 100
            point_return = point_return.fillna(0)
            bar_colors = [COLORS["info"] if r >= 0 else COLORS["negative"] for r in point_return]
            fig_returns = go.Figure()
            fig_returns.add_trace(go.Bar(
                x=x_vals,
                y=point_return.values,
                marker_color=bar_colors,
                name="Return %"
            ))
            fig_returns.update_layout(
                template="plotly_dark",
                height=max(int(CHART_HEIGHTS.get("equity", 500) * 0.35), 160),
                margin=dict(l=10, r=10, t=10, b=10),
                showlegend=False,
                xaxis=dict(type="date"),
            )
            fig_returns.update_yaxes(title_text="Return %", ticksuffix="%", zeroline=True, zerolinecolor="#444")

            # region agent log
            _debug_log(
                "H13",
                "charts.py:214",
                "Plotly return chart ready",
                {
                    "trace_count": len(fig_returns.data),
                    "layout_height": fig_returns.layout.height,
                },
            )
            # endregion
            st.plotly_chart(fig_returns, use_container_width=True, key="equity_returns_plotly")
        return

    line_df = equity_df[["datetime", "close"]].set_index("datetime")
    base_close = float(line_df["close"].iloc[0]) if not line_df.empty else None
    if base_close and base_close != 0:
        line_df["close"] = (line_df["close"] / base_close - 1.0) * 100.0
    # region agent log
    _debug_log(
        "H8",
        "charts.py:182",
        "Simple chart data prepared",
        {
            "rows": len(line_df),
            "first_dt": str(line_df.index[0]) if not line_df.empty else None,
            "last_dt": str(line_df.index[-1]) if not line_df.empty else None,
            "close_min": float(line_df["close"].min()) if not line_df.empty else None,
            "close_max": float(line_df["close"].max()) if not line_df.empty else None,
            "close_range": float(line_df["close"].max() - line_df["close"].min()) if not line_df.empty else None,
            "base_close": base_close,
        },
    )
    # endregion

    st.line_chart(line_df, height=CHART_HEIGHTS.get("equity", 350))

    point_return = equity_df["close"].pct_change().fillna(0)
    # region agent log
    _debug_log(
        "H9",
        "charts.py:195",
        "Return series stats",
        {
            "ret_min": float(point_return.min()) if not point_return.empty else None,
            "ret_max": float(point_return.max()) if not point_return.empty else None,
            "ret_abs_max": float(point_return.abs().max()) if not point_return.empty else None,
        },
    )
    # endregion
    return_df = point_return.to_frame(name="return")
    st.bar_chart(return_df, height=max(int(CHART_HEIGHTS.get("equity", 350) * 0.35), 120))


def render_benchmark_chart(equity_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> None:
    if equity_df.empty:
        st.info("Waiting for equity data...")
        return
    eq = equity_df.copy().sort_values("datetime")
    eq_start = eq["close"].iloc[0]
    eq_norm = eq["close"] / eq_start if eq_start else eq["close"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["datetime"],
        y=eq_norm,
        mode="lines+markers",
        name="Strategy",
        line=dict(color=COLORS["positive"], width=2),
        marker=dict(size=4)
    ))

    if not benchmark_df.empty and benchmark_df["close"].iloc[0] > 0:
        b_start = benchmark_df["close"].iloc[0]
        b_norm = benchmark_df["close"] / b_start
        fig.add_trace(go.Scatter(
            x=benchmark_df["datetime"],
            y=b_norm,
            mode="lines",
            name="Benchmark",
            line=dict(color="#888888", width=2, dash="dot")
        ))

    fig.update_layout(
        template="plotly_dark",
        height=CHART_HEIGHTS.get("benchmark", 450),
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_margin_chart(margin_df: pd.DataFrame) -> None:
    if margin_df.empty:
        st.info("No margin data logged yet.")
        return
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=margin_df["datetime"],
        y=margin_df["margin_used"],
        mode="lines",
        name="Margin Used",
        line=dict(color=COLORS["warning"], width=2)
    ))
    fig.update_layout(
        template="plotly_dark",
        height=CHART_HEIGHTS.get("margin", 450),
        margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_insights_barchart(insights_df: pd.DataFrame) -> None:
    if insights_df.empty:
        st.info("No Alpha Insights")
        return
    if "symbol" not in insights_df.columns or "direction" not in insights_df.columns:
        st.dataframe(insights_df, use_container_width=True)
        return
    counts = insights_df.groupby(["symbol", "direction"]).size().reset_index(name="count")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=counts["symbol"],
        y=counts["count"],
        marker_color=COLORS["info"],
        name="Insights"
    ))
    fig.update_layout(
        template="plotly_dark",
        height=CHART_HEIGHTS.get("aligned", 500),
        margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_aligned_chart(equity_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> None:
    if equity_df.empty or benchmark_df.empty:
        st.info("Aligned chart needs both equity and benchmark data.")
        return
    render_benchmark_chart(equity_df, benchmark_df)
