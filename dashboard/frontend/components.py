"""
UI Components for LEAN Live Trading Dashboard
Contains reusable Streamlit components and layout elements.
"""

from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from .. import config
from ..backend.data_processor import load_equity_data

ORDER_STATUS = config.ORDER_STATUS
ORDER_DIRECTION = config.ORDER_DIRECTION
COLORS = config.COLORS


def get_global_styles() -> str:
    return """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&family=Space+Grotesk:wght@400;500;600&display=swap');

    :root {
        --bg: #f7f7f5;
        --surface: #ffffff;
        --border: #e6e6e2;
        --text: #1a1a1a;
        --text-secondary: #6b6b6b;
        --accent: #2563eb;
        --positive: #1f7a6d;
        --negative: #b42318;
        --shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
    }

    html, body, [data-testid="stAppViewContainer"] {
        background: var(--bg) !important;
        color: var(--text);
        font-family: 'IBM Plex Sans', system-ui, -apple-system, sans-serif;
    }

    [data-testid="stHeader"] {
        background: transparent;
    }

    [data-testid="stSidebar"] {
        background: var(--surface);
        border-right: 1px solid var(--border);
    }

    .stMarkdown, .stText, .stCaption, .stDataFrame, .stTable {
        color: var(--text);
    }

    h1, h2, h3, h4, h5, h6 {
        font-family: 'Space Grotesk', system-ui, -apple-system, sans-serif;
        color: var(--text);
        letter-spacing: -0.02em;
    }

    .metrics-row {
        display: grid;
        grid-template-columns: repeat(7, minmax(120px, 1fr));
        gap: 12px;
        margin-bottom: 18px;
    }

    .metrics-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 12px 14px;
        box-shadow: var(--shadow);
    }

    .metrics-label {
        font-size: 0.78rem;
        color: var(--text-secondary);
        margin-bottom: 6px;
    }

    .metrics-value {
        font-size: 1.05rem;
        font-weight: 600;
        color: var(--text);
    }

    .panel-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: var(--shadow);
    }

    .panel-title {
        font-size: 0.9rem;
        font-weight: 600;
        margin-bottom: 10px;
        color: var(--text);
    }

    [data-testid="stTabs"] button {
        font-weight: 500;
        color: var(--text-secondary);
    }

    [data-testid="stTabs"] button[aria-selected="true"] {
        color: var(--text);
        border-bottom: 2px solid var(--accent);
    }

    .stRadio [role="radiogroup"] {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 6px;
        display: flex;
        gap: 6px;
    }

    .stRadio label {
        margin: 0 !important;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 0.85rem;
        color: var(--text-secondary);
    }

    .stRadio label:has(input:checked) {
        background: var(--bg);
        color: var(--text);
        border: 1px solid var(--border);
    }

    .stButton > button {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 8px;
        color: var(--text);
        font-weight: 500;
        padding: 6px 12px;
    }

    .stButton > button:hover {
        border-color: #d0d0cb;
        background: #fbfbfa;
    }
    </style>
    """


def inject_global_styles() -> None:
    st.markdown(get_global_styles(), unsafe_allow_html=True)


def build_metrics_bar_html(equity: str, fees: str, holdings: str, net_profit: str, psr: str, unrealized: str, cash: str) -> str:
    items = [
        ("Equity", equity, "positive"),
        ("Fees", fees, "negative"),
        ("Holdings", holdings, "positive"),
        ("Net Profit", net_profit, "neutral"),
        ("PSR", psr, "neutral"),
        ("Unrealized", unrealized, "neutral"),
        ("Cash", cash, "neutral"),
    ]
    cards = []
    for label, value, tone in items:
        color = COLORS.get(tone, COLORS["text"])
        cards.append(
            f"""
            <div class="metrics-card">
                <div class="metrics-label">{label}</div>
                <div class="metrics-value" style="color:{color};">{value}</div>
            </div>
            """
        )
    return f"<div class='metrics-row'>{''.join(cards)}</div>"


def render_metrics_bar(results: Dict) -> None:
    holdings = results.get("holdings", {}) if isinstance(results, dict) else {}
    cash = results.get("cash", {}).get("USD", {}).get("amount", 0)

    total_value = 0.0
    total_unrealized = 0.0
    for symbol, h in holdings.items():
        if "v" in h:
            total_value += h.get("v", 0)
        qty = float(h.get("q", 0) or 0)
        avg = float(h.get("a", 0) or 0)
        cur = float(h.get("p", 0) or 0)
        if qty != 0 and avg > 0:
            total_unrealized += (cur - avg) * qty
        elif "u" in h:
            total_unrealized += h.get("u", 0)

    equity = total_value + cash
    runtime = results.get("runtimeStatistics", {}) if isinstance(results, dict) else {}
    fees = runtime.get("Fees", "$0.00")
    net_profit = runtime.get("Net Profit", "$0.00")
    psr = runtime.get("Probabilistic Sharpe Ratio", "0%")

    def fmt_dol(val):
        return f"${val:,.2f}"

    html = build_metrics_bar_html(
        equity=fmt_dol(equity),
        fees=fees,
        holdings=fmt_dol(total_value),
        net_profit=net_profit,
        psr=psr,
        unrealized=fmt_dol(total_unrealized),
        cash=fmt_dol(cash),
    )
    st.markdown(html, unsafe_allow_html=True)


def render_server_stats_box(stats: Dict) -> None:
    st.markdown(
        f"""
        <div class="panel-card">
            <div class="panel-title">Server Statistics</div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>CPU</span><span>{stats.get('cpu',0)}%</span></div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>RAM</span><span>{stats.get('ram_used',0)} MB / {stats.get('ram_total',0)} MB</span></div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>Host</span><span>Local</span></div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>Uptime</span><span>{stats.get('uptime','')}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def get_current_market_prices(holdings: Dict) -> Dict[str, float]:
    prices = {}
    for symbol, h in holdings.items():
        try:
            prices[symbol] = float(h.get("p", 0) or 0)
        except Exception:
            prices[symbol] = 0.0
    return prices


def render_holdings_dataframe(holdings: Dict) -> pd.DataFrame:
    if isinstance(holdings, list):
        holdings = {h.get("symbol", f"SYM{idx}"): h for idx, h in enumerate(holdings)}
    rows = []
    for symbol, h in holdings.items():
        qty = float(h.get("q", 0) or 0)
        avg = float(h.get("a", 0) or 0)
        cur = float(h.get("p", 0) or 0)
        val = float(h.get("v", 0) or 0)
        pnl = (cur - avg) * qty if qty != 0 and avg != 0 else float(h.get("u", 0) or 0)
        pnl_pct = ((cur / avg - 1) * 100) if avg != 0 else 0
        rows.append({
            "Symbol": symbol,
            "Qty": qty,
            "Avg Price": avg,
            "Current": cur,
            "Value": val,
            "P&L $": pnl,
            "P&L %": pnl_pct,
        })
    return pd.DataFrame(rows)


def render_holdings_table(holdings: Dict) -> None:
    df = render_holdings_dataframe(holdings)
    if df.empty:
        st.info("No Current Holdings")
        return
    st.dataframe(df, use_container_width=True)


def render_orders_dataframe(orders: Dict) -> pd.DataFrame:
    rows = []
    for oid, order in (orders or {}).items():
        rows.append({
            "Id": oid,
            "Symbol": order.get("symbol"),
            "Status": ORDER_STATUS.get(order.get("status"), order.get("status")),
            "Direction": ORDER_DIRECTION.get(order.get("direction"), order.get("direction")),
            "Qty": order.get("quantity"),
            "Price": order.get("price"),
        })
    return pd.DataFrame(rows)


def render_order_events_dataframe(events: List[Dict]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame()
    return pd.DataFrame(events)


def render_insights_dataframe(insights: List[Dict]) -> pd.DataFrame:
    if not insights:
        return pd.DataFrame()
    return pd.DataFrame(insights)


def render_session_selector(sessions: List[str], current_session: Optional[str]) -> str:
    if not sessions:
        return ""
    default_index = 0
    if current_session in sessions:
        default_index = sessions.index(current_session)
    return st.selectbox("Session", sessions, index=default_index)


def render_settings_panel() -> Tuple[bool, int, bool]:
    with st.expander("Settings", expanded=False):
        auto_refresh = st.checkbox("Auto-Refresh", value=False)
        refresh_rate = st.slider("Rate (s)", 5, 60, 10)
        manual_refresh = st.button("Manual Refresh", use_container_width=True)
    return auto_refresh, refresh_rate, manual_refresh


def render_stop_button(container_id: str, check_container_running, terminate_container) -> None:
    if container_id and check_container_running(container_id):
        if st.button("Stop Algo", type="primary", use_container_width=True):
            success, msg = terminate_container(container_id)
            if success:
                st.success("Stopped")
            else:
                st.error(msg or "Failed")


def render_chart_selector() -> str:
    return st.radio("View", ["Strategy Equity", "Benchmark", "Portfolio Margin"], label_visibility="collapsed")
