"""
Main Streamlit application for LEAN Live Trading Dashboard
"""

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from . import config
    from .backend.data_loader import (
        get_live_sessions,
        load_config,
        load_results,
        load_order_events,
        load_alpha_insights,
        load_log_tail,
        parse_margin_from_logs,
        fetch_benchmark_data,
        parse_server_stats,
        load_example_account,
        load_example_positions,
        load_example_orders,
        load_example_insights,
        load_example_logs,
        load_example_benchmarks,
        load_example_equity,
        load_example_server_stats,
    )
    from .backend.data_processor import load_equity_data, get_all_orders_from_logs, get_all_insights_from_logs
    from .backend.session_manager import terminate_container, check_container_running
    from .frontend.components import (
        render_metrics_bar,
        render_server_stats_box,
        render_holdings_table,
        render_orders_dataframe,
        render_order_events_dataframe,
        render_insights_dataframe,
        render_session_selector,
        render_settings_panel,
        render_stop_button,
        render_chart_selector,
    )
    from .frontend.charts import (
        render_equity_chart,
        render_benchmark_chart,
        render_margin_chart,
        render_insights_barchart,
    )
    from .frontend.tables import (
        render_orders_table,
        render_insights_table,
        render_runtime_stats_table,
        render_log_viewer,
        render_error_summary,
        render_margin_data_table,
    )
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from dashboard import config
    from dashboard.backend.data_loader import (
        get_live_sessions,
        load_config,
        load_results,
        load_order_events,
        load_alpha_insights,
        load_log_tail,
        parse_margin_from_logs,
        fetch_benchmark_data,
        parse_server_stats,
        load_example_account,
        load_example_positions,
        load_example_orders,
        load_example_insights,
        load_example_logs,
        load_example_benchmarks,
        load_example_equity,
        load_example_server_stats,
    )
    from dashboard.backend.data_processor import load_equity_data, get_all_orders_from_logs, get_all_insights_from_logs
    from dashboard.backend.session_manager import terminate_container, check_container_running
    from dashboard.frontend.components import (
        render_metrics_bar,
        render_server_stats_box,
        render_holdings_table,
        render_orders_dataframe,
        render_order_events_dataframe,
        render_insights_dataframe,
        render_session_selector,
        render_settings_panel,
        render_stop_button,
        render_chart_selector,
    )
    from dashboard.frontend.charts import (
        render_equity_chart,
        render_benchmark_chart,
        render_margin_chart,
        render_insights_barchart,
    )
    from dashboard.frontend.tables import (
        render_orders_table,
        render_insights_table,
        render_runtime_stats_table,
        render_log_viewer,
        render_error_summary,
        render_margin_data_table,
    )

LOG_PATH = Path(r"e:\factor\lean_project\Pensive Tan Bull Local\.cursor\debug.log")


def _debug_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": "debug-session",
            "runId": "env-check",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception:
        pass

def main() -> None:
    st.set_page_config(**config.PAGE_CONFIG)

    # Ensure Plotly candlestick defaults for deployments without env vars
    os.environ.setdefault("DASHBOARD_USE_PLOTLY", "1")
    os.environ.setdefault("DASHBOARD_PLOTLY_CANDLE", "1")
    os.environ.setdefault("DASHBOARD_PLOTLY_HTML", "1")
    os.environ.setdefault("DASHBOARD_PLOTLY_LINE", "1")
    _debug_log(
        "H22",
        "app.py:140",
        "Plotly env defaults",
        {
            "DASHBOARD_USE_PLOTLY": os.getenv("DASHBOARD_USE_PLOTLY"),
            "DASHBOARD_PLOTLY_CANDLE": os.getenv("DASHBOARD_PLOTLY_CANDLE"),
            "DASHBOARD_PLOTLY_HTML": os.getenv("DASHBOARD_PLOTLY_HTML"),
            "DASHBOARD_PLOTLY_LINE": os.getenv("DASHBOARD_PLOTLY_LINE"),
        },
    )

    example_mode = os.getenv("DASHBOARD_EXAMPLE_MODE", "1" if config.DEFAULT_EXAMPLE_MODE else "0") == "1"
    if example_mode:
        sessions = ["example"]
        selected_session = "example"
    else:
        sessions = get_live_sessions()
        if not sessions:
            st.error("No live sessions found in 'live/' folder")
            return
        selected_session = sessions[0]

    col_head1, col_head2 = st.columns([6, 1])
    with col_head1:
        st.caption(f"Active session: {selected_session}")
    with col_head2:
        auto_refresh, refresh_rate, manual_refresh = render_settings_panel()
        if manual_refresh:
            st.rerun()

    if example_mode:
        account = load_example_account()
        positions = load_example_positions()
        results = {
            "runtimeStatistics": account.get("runtimeStatistics", {}),
            "cash": {"USD": {"amount": account.get("cash", 0)}},
            "holdings": {p.get("symbol", f"SYM{idx}"): p for idx, p in enumerate(positions)},
            "orders": {str(o.get("id", idx)): o for idx, o in enumerate(load_example_orders())},
        }
        session_config = {}
    else:
        session_config = load_config(selected_session)
        results = load_results(selected_session)

        if not results:
            st.warning(f"Session {selected_session} initializing... please wait.")
            if auto_refresh:
                time.sleep(refresh_rate)
                st.rerun()
            return

    render_metrics_bar(results)

    col_main, col_side = st.columns([3, 1])

    with col_side:
        selected_chart = render_chart_selector()
        if example_mode:
            server_stats = load_example_server_stats() or {
                "cpu": 0,
                "ram_used": 0,
                "ram_total": 0,
                "host": "Local",
                "uptime": "0d 00:00:00",
            }
        else:
            server_stats = parse_server_stats(selected_session)
        render_server_stats_box(server_stats)
        if session_config:
            container_id = session_config.get("container", "")
            render_stop_button(container_id, check_container_running, terminate_container)

    with col_main:
        st.markdown(f"##### {selected_chart}")
        if example_mode:
            try:
                equity_df = pd.DataFrame(load_example_equity())
            except Exception:
                equity_df = pd.DataFrame()
        else:
            equity_df = load_equity_data(results)

        if example_mode:
            with st.expander("Example Data Debug", expanded=False):
                st.caption(f"Equity points: {len(equity_df)}")
                if not equity_df.empty:
                    st.dataframe(equity_df.head(10), use_container_width=True)

        if selected_chart == "Strategy Equity":
            if example_mode:
                try:
                    equity_df = pd.read_json(config.EXAMPLE_DATA_DIR / "equity.json")
                except Exception as exc:
                    equity_df = pd.DataFrame(load_example_equity())
            render_equity_chart(equity_df)
            if not equity_df.empty:
                current_equity = equity_df["close"].iloc[-1]
                st.caption(f"Current: ${current_equity:,.2f} | {len(equity_df)} points")
        elif selected_chart == "Benchmark":
            benchmark_df = pd.DataFrame(load_example_benchmarks()) if example_mode else fetch_benchmark_data(selected_session)
            render_benchmark_chart(equity_df, benchmark_df)
        else:
            margin_data = [] if example_mode else parse_margin_from_logs(selected_session)
            if margin_data:
                render_margin_chart(pd.DataFrame(margin_data))
            else:
                st.info("No margin data logged yet.")

    st.markdown("---")
    tabs = st.tabs(["Overview", "Report", "Orders", "Insights", "Logs"])

    with tabs[0]:
        render_holdings_table(results.get("holdings", {}))

    with tabs[1]:
        render_runtime_stats_table(results.get("runtimeStatistics", {}))

    with tabs[2]:
        orders_df = render_orders_dataframe(results.get("orders", {}))
        render_orders_table(orders_df)
        events_df = render_order_events_dataframe(load_example_orders() if example_mode else load_order_events(selected_session))
        if not events_df.empty:
            st.markdown("### Order Events")
            st.dataframe(events_df, use_container_width=True, height=250)

    with tabs[3]:
        insights_df = render_insights_dataframe(load_example_insights() if example_mode else load_alpha_insights(selected_session))
        render_insights_table(insights_df)
        if not insights_df.empty:
            render_insights_barchart(insights_df)

    with tabs[4]:
        log_lines = st.number_input("Count", 50, 2000, config.DEFAULT_LOG_LINES, label_visibility="collapsed")
        if example_mode:
            logs = "\n".join(load_example_logs())
        else:
            logs = load_log_tail(selected_session, int(log_lines))
        render_log_viewer(logs, int(log_lines))

    if auto_refresh:
        time.sleep(refresh_rate)
        st.rerun()


if __name__ == "__main__":
    main()
