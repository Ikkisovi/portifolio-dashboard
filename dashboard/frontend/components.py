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

    st.markdown(
        f"""
        <div style="display:flex;justify-content:space-between;background:{COLORS['background']};padding:10px 15px;border-bottom:1px solid #333;border-radius:4px;margin-bottom:20px;">
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">Equity</div><div style="color:{COLORS['positive']};font-size:1.1rem;font-weight:600;">{fmt_dol(equity)}</div></div>
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">Fees</div><div style="color:{COLORS['negative']};font-size:1.1rem;font-weight:600;">{fees}</div></div>
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">Holdings</div><div style="color:{COLORS['positive']};font-size:1.1rem;font-weight:600;">{fmt_dol(total_value)}</div></div>
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">Net Profit</div><div style="color:{COLORS['text']};font-size:1.1rem;font-weight:600;">{net_profit}</div></div>
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">PSR</div><div style="color:{COLORS['text']};font-size:1.1rem;font-weight:600;">{psr}</div></div>
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">Unrealized</div><div style="color:{COLORS['positive'] if total_unrealized>=0 else COLORS['negative']};font-size:1.1rem;font-weight:600;">{fmt_dol(total_unrealized)}</div></div>
            <div><div style="color:{COLORS['text_secondary']};font-size:0.85rem;">Cash</div><div style="color:{COLORS['text']};font-size:1.1rem;font-weight:600;">{fmt_dol(cash)}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_server_stats_box(stats: Dict) -> None:
    st.markdown(
        f"""
        <div style="background:{COLORS['surface']};border:1px solid #333;border-radius:4px;padding:15px;margin-bottom:15px;">
            <div style="color:{COLORS['text']};font-weight:600;border-bottom:1px solid #444;padding-bottom:8px;margin-bottom:10px;font-size:0.95rem;">Server Statistics</div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>CPU</span><span>{stats.get('cpu',0)}%</span></div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>RAM</span><span>{stats.get('ram_used',0)} MB / {stats.get('ram_total',0)} MB</span></div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>Host</span><span>Local</span></div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:0.9rem;color:{COLORS['text_secondary']};"><span>Up Time</span><span>{stats.get('uptime','')}</span></div>
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
    with st.expander("?? Settings", expanded=False):
        auto_refresh = st.checkbox("Auto-Refresh", value=False)
        refresh_rate = st.slider("Rate (s)", 5, 60, 10)
        manual_refresh = st.button("Manual Refresh", use_container_width=True)
    return auto_refresh, refresh_rate, manual_refresh


def render_stop_button(container_id: str, check_container_running, terminate_container) -> None:
    if container_id and check_container_running(container_id):
        if st.button("?? STOP ALGO", type="primary", use_container_width=True):
            success, msg = terminate_container(container_id)
            if success:
                st.success("Stopped")
            else:
                st.error(msg or "Failed")


def render_chart_selector() -> str:
    return st.radio("Select View", ["Strategy Equity", "Benchmark", "Portfolio Margin"], label_visibility="collapsed")
