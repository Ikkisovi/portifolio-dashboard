import pandas as pd


def _date_text(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _rank_rows(series: pd.Series, top_n: int, ascending: bool) -> list:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return []
    ranked = sorted(
        ((str(symbol), float(value)) for symbol, value in numeric.items()),
        key=lambda item: (item[1], item[0]) if ascending else (-item[1], item[0]),
    )[: max(1, int(top_n))]
    return [
        {
            "symbol": symbol,
            "score": round(value, 6),
            "rank": int(rank),
        }
        for rank, (symbol, value) in enumerate(ranked, start=1)
    ]


def _sorted_panel(panel: pd.DataFrame) -> pd.DataFrame:
    if panel is None or not isinstance(panel, pd.DataFrame) or panel.empty:
        return pd.DataFrame()
    return panel.sort_index()


def _latest_panel_date(panel: pd.DataFrame) -> str:
    sorted_panel = _sorted_panel(panel)
    if sorted_panel.empty:
        return ""
    return _date_text(sorted_panel.index[-1])


def _resolve_data_as_of_date(data_as_of_date, *panel_maps: dict) -> str:
    latest_dates = []
    for panel_map in panel_maps:
        for panel in list((panel_map or {}).values()):
            latest_date = _latest_panel_date(panel)
            if latest_date:
                latest_dates.append(latest_date)
    if latest_dates:
        # Use the earliest factor snapshot date so partially stale panel refreshes stay visible.
        return min(latest_dates)
    return _date_text(data_as_of_date)


def _history_rows(panel: pd.DataFrame, selection_size: int, lookback_days: int) -> list:
    if panel is None or panel.empty:
        return []
    sorted_panel = _sorted_panel(panel)
    history_dates = pd.Index([_date_text(idx) for idx in sorted_panel.index], dtype="object")
    keep_mask = ~history_dates.duplicated(keep="last")
    deduped_panel = sorted_panel.loc[keep_mask]
    deduped_dates = history_dates[keep_mask]
    rows = []
    for date_text, (_, row) in zip(deduped_dates, deduped_panel.iterrows()):
        numeric = pd.to_numeric(row, errors="coerce").dropna()
        if numeric.empty:
            continue
        rows.append(
            {
                "date": date_text,
                "top": _rank_rows(numeric, top_n=selection_size, ascending=False),
                "bottom": _rank_rows(numeric, top_n=selection_size, ascending=True),
            }
        )
    return rows[-max(1, int(lookback_days)) :]


def _context_map(context_rows: list) -> dict:
    out = {}
    for row in list(context_rows or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("factor_name") or "")
        if name:
            out[name] = row
    return out


def _build_factor_rows(
    factor_panels: dict,
    factor_context: list,
    selection_size: int,
    lookback_days: int,
    include_effective_direction: bool,
) -> list:
    ctx_map = _context_map(factor_context)
    factor_names = []
    for row in list(factor_context or []):
        if isinstance(row, dict):
            name = str(row.get("name") or row.get("factor_name") or "")
            if name and name not in factor_names:
                factor_names.append(name)
    for name in list((factor_panels or {}).keys()):
        if name not in factor_names:
            factor_names.append(str(name))

    results = []
    for name in factor_names:
        panel = (factor_panels or {}).get(name)
        if panel is None or not isinstance(panel, pd.DataFrame) or panel.empty:
            continue
        latest_row = pd.to_numeric(panel.iloc[-1], errors="coerce").dropna()
        if latest_row.empty:
            continue
        meta = dict(ctx_map.get(name) or {})
        row = {
            "name": name,
            "expr": str(meta.get("expr") or ""),
            "expr_hash": str(meta.get("expr_hash") or ""),
            "latest": {
                "top": _rank_rows(latest_row, top_n=selection_size, ascending=False),
                "bottom": _rank_rows(latest_row, top_n=selection_size, ascending=True),
            },
            "history_20d": _history_rows(panel, selection_size=selection_size, lookback_days=lookback_days),
        }
        if include_effective_direction:
            row["effective_direction"] = int(meta.get("effective_direction", 1) or 1)
        results.append(row)
    return results


def build_factor_selection_daily_report(
    as_of_date,
    data_as_of_date,
    source_mode: str,
    cma_factor_panels: dict,
    cma_factor_context: list,
    actionable_factor_panels: dict,
    actionable_factor_context: list,
    selection_size: int = 10,
    lookback_days: int = 20,
) -> dict:
    selection_size = max(1, int(selection_size))
    lookback_days = max(1, int(lookback_days))
    resolved_data_as_of_date = _resolve_data_as_of_date(
        data_as_of_date,
        cma_factor_panels,
        actionable_factor_panels,
    )
    return {
        "as_of_date": _date_text(as_of_date),
        "data_as_of_date": resolved_data_as_of_date,
        "source_mode": str(source_mode or ""),
        "lookback_days": lookback_days,
        "selection_size": selection_size,
        "cma_factors": _build_factor_rows(
            factor_panels=cma_factor_panels,
            factor_context=cma_factor_context,
            selection_size=selection_size,
            lookback_days=lookback_days,
            include_effective_direction=True,
        ),
        "actionable_factors": _build_factor_rows(
            factor_panels=actionable_factor_panels,
            factor_context=actionable_factor_context,
            selection_size=selection_size,
            lookback_days=lookback_days,
            include_effective_direction=False,
        ),
    }
