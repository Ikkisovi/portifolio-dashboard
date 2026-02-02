"""
Table rendering functions for LEAN Live Trading Dashboard
Handles DataFrame display and formatting.
"""

import pandas as pd
import streamlit as st

from .. import config

ORDER_STATUS = config.ORDER_STATUS
ORDER_DIRECTION = config.ORDER_DIRECTION


def render_orders_table(orders_df: pd.DataFrame) -> None:
    if orders_df.empty:
        st.info("No Orders Generated")
        return
    st.dataframe(orders_df, use_container_width=True, height=300)


def render_insights_table(insights_df: pd.DataFrame) -> None:
    if insights_df.empty:
        st.info("No Alpha Insights")
        return
    st.dataframe(insights_df, use_container_width=True)


def render_runtime_stats_table(runtime_stats: dict) -> None:
    if not runtime_stats:
        st.info("Runtime Statistics Report")
        return
    rows = [{"Metric": k, "Value": v} for k, v in runtime_stats.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


def render_log_viewer(log_content: str, lines: int = 200) -> None:
    st.code(log_content, language="text")


def render_session_list_table(sessions_status: list) -> None:
    if not sessions_status:
        st.info("No sessions found")
        return
    st.dataframe(pd.DataFrame(sessions_status), use_container_width=True)


def render_error_summary(errors: list) -> None:
    if not errors:
        st.info("No errors detected")
        return
    st.dataframe(pd.DataFrame({"Error": errors}), use_container_width=True)


def render_margin_data_table(margin_data: list) -> None:
    if not margin_data:
        st.info("No margin data logged yet.")
        return
    st.dataframe(pd.DataFrame(margin_data), use_container_width=True)


def render_benchmark_data_table(benchmark_df: pd.DataFrame) -> None:
    if benchmark_df.empty:
        st.info("No benchmark data")
        return
    st.dataframe(benchmark_df, use_container_width=True)
