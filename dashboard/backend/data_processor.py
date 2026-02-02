"""
Data processing and calculation functions for LEAN Live Trading Dashboard
Handles computations, data transformations, and business logic.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import pandas as pd

from .. import config
from . import models
from .data_loader import (
    LIVE_PATH,
    load_equity_cache,
    parse_lean_timestamp,
    get_live_sessions,
    parse_orders_from_logs,
    parse_insights_from_logs,
    load_example_equity,
)

DEFAULT_CACHE_MAX_POINTS = config.DEFAULT_CACHE_MAX_POINTS
DEFAULT_EXAMPLE_MODE = config.DEFAULT_EXAMPLE_MODE


def compute_cash_total(data: Dict) -> Optional[float]:
    cashbook = data.get("cash", {}) if isinstance(data, dict) else {}
    if not isinstance(cashbook, dict) or not cashbook:
        return None

    cash_total = 0.0
    has_cash = False
    for cash in cashbook.values():
        if not isinstance(cash, dict):
            continue
        if "valueInAccountCurrency" in cash:
            try:
                cash_total += float(cash.get("valueInAccountCurrency", 0) or 0)
                has_cash = True
            except (TypeError, ValueError):
                continue
        elif "amount" in cash:
            try:
                cash_total += float(cash.get("amount", 0) or 0)
                has_cash = True
            except (TypeError, ValueError):
                continue
    return cash_total if has_cash else None


def compute_holdings_value(data: Dict, last_prices: Dict[str, float]) -> Tuple[float, bool, Dict[str, float]]:
    holdings = data.get("holdings", {}) if isinstance(data, dict) else {}
    holdings_total = 0.0
    has_position = False

    if not isinstance(holdings, dict):
        return holdings_total, has_position, last_prices

    for symbol, h in holdings.items():
        if not isinstance(h, dict):
            continue

        q = h.get("q")
        p = h.get("p")
        v = h.get("v")

        if p is not None:
            try:
                last_prices[symbol] = float(p)
            except (TypeError, ValueError):
                pass

        if q is None:
            if v is not None:
                try:
                    v_val = float(v)
                    holdings_total += v_val
                    if v_val != 0:
                        has_position = True
                except (TypeError, ValueError):
                    pass
            continue

        try:
            q_val = float(q)
        except (TypeError, ValueError):
            continue

        if q_val != 0:
            has_position = True

        price = last_prices.get(symbol)
        if price is None and v is not None and q_val != 0:
            try:
                price = float(v) / q_val
                last_prices[symbol] = price
            except (TypeError, ValueError, ZeroDivisionError):
                price = None
        if price is None:
            a = h.get("a")
            if a is not None:
                try:
                    price = float(a)
                    last_prices[symbol] = price
                except (TypeError, ValueError):
                    price = None
        if price is None:
            continue

        holdings_total += q_val * price

    return holdings_total, has_position, last_prices


def get_factor_names(factor_cache: Dict) -> List[str]:
    if not isinstance(factor_cache, dict):
        return []
    return sorted(factor_cache.keys())


def load_factor_values_frame(factor_cache: Dict) -> pd.DataFrame:
    if not isinstance(factor_cache, dict) or not factor_cache:
        return pd.DataFrame()
    try:
        df = pd.DataFrame(factor_cache)
        return df
    except Exception:
        return pd.DataFrame()


def load_factor_series(factor_cache: Dict, series_key: str) -> pd.Series:
    if not isinstance(factor_cache, dict):
        return pd.Series(dtype=float)
    series = factor_cache.get(series_key, [])
    try:
        return pd.Series(series)
    except Exception:
        return pd.Series(dtype=float)


def build_factor_daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "datetime" not in df.columns:
        return pd.DataFrame()
    daily = df.set_index("datetime").resample("D").mean(numeric_only=True)
    return daily.reset_index()


def build_account_snapshot(results: Dict, account_currency: str = "USD") -> models.AccountSnapshot:
    cash_total = compute_cash_total(results) or 0.0
    holdings_total = 0.0
    positions: List[models.HoldingPosition] = []

    holdings = results.get("holdings", {}) if isinstance(results, dict) else {}
    for symbol, h in holdings.items():
        qty = float(h.get("q", 0) or 0)
        avg = float(h.get("a", 0) or 0)
        price = float(h.get("p", 0) or 0)
        value = float(h.get("v", 0) or 0)
        unrealized = float(h.get("u", 0) or 0)
        unrealized_pct = float(h.get("up", 0) or 0)
        fx_rate = float(h.get("fx", 1) or 1)
        holdings_total += value
        positions.append(models.HoldingPosition(
            symbol=symbol,
            quantity=qty,
            average_price=avg,
            price=price,
            value=value,
            unrealized=unrealized,
            unrealized_pct=unrealized_pct,
            fx_rate=fx_rate,
        ))

    equity = cash_total + holdings_total
    snapshot = models.AccountSnapshot(
        account_currency=account_currency,
        cash_total=cash_total,
        invested=holdings_total,
        equity=equity,
        unrealized=sum(p.unrealized for p in positions),
    )
    snapshot.positions = positions
    return snapshot


def update_equity_cache(session: str, equity_value: float, at_time: Optional[datetime] = None, max_points: int = DEFAULT_CACHE_MAX_POINTS) -> None:
    session_path = LIVE_PATH / session
    if not session_path.exists():
        return
    cache_path = session_path / config.EQUITY_CACHE_FILE
    points = []
    if cache_path.exists():
        try:
            points = json.loads(cache_path.read_text())
        except Exception:
            points = []
    if at_time is None:
        at_time = datetime.now()
    points.append({"datetime": at_time.isoformat(), "close": equity_value})
    if len(points) > max_points:
        points = points[-max_points:]
    try:
        cache_path.write_text(json.dumps(points))
    except Exception:
        pass


def load_equity_data(results: Dict = None, session: str = None) -> pd.DataFrame:
    if os.getenv("DASHBOARD_EXAMPLE_MODE", "1" if DEFAULT_EXAMPLE_MODE else "0") == "1":
        data = load_example_equity()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        if "close" in df.columns:
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["datetime", "close"]).sort_values("datetime")
        if df.empty:
            return pd.DataFrame()
        if not {"open", "high", "low"}.issubset(df.columns):
            df["open"] = df["close"]
            df["high"] = df["close"]
            df["low"] = df["close"]
        return df[["datetime", "open", "high", "low", "close"]]

    data_points: List[Dict] = []

    if not LIVE_PATH.exists():
        return pd.DataFrame()

    for session_path in sorted(LIVE_PATH.iterdir()):
        if not session_path.is_dir():
            continue

        series_files = list(session_path.glob("L-*minute.json"))
        series_files.extend(session_path.glob("L-*_second_Strategy%20Equity.json"))

        file_entries = []
        for mf in series_files:
            try:
                data = json.loads(mf.read_text())
                file_time = datetime.fromtimestamp(mf.stat().st_mtime)
                state = data.get("state", {}) if isinstance(data, dict) else {}
                end_time = state.get("EndTime") if isinstance(state, dict) else None
                timestamp = parse_lean_timestamp(end_time) if end_time else None
                if timestamp is None:
                    timestamp = file_time
                file_entries.append((timestamp, file_time, data, mf.name))
            except Exception:
                continue

        file_entries.sort(key=lambda x: (x[0], x[1]))

        last_prices: Dict[str, float] = {}
        last_cash: Optional[float] = None
        started = False

        for timestamp, file_time, data, name in file_entries:
            cash_total = compute_cash_total(data)
            if cash_total is None:
                cash_total = last_cash if last_cash is not None else 0.0
            else:
                last_cash = cash_total

            holdings_total, has_position, last_prices = compute_holdings_value(data, last_prices)
            equity_val = cash_total + holdings_total

            if not started:
                if has_position:
                    started = True
                else:
                    continue

            if equity_val <= 0:
                continue

            data_points.append({
                "datetime": timestamp,
                "open": equity_val,
                "high": equity_val,
                "low": equity_val,
                "close": equity_val,
                "source": str(name),
                "mtime": file_time,
            })

    if not data_points:
        return pd.DataFrame()

    df = pd.DataFrame(data_points)
    df = df.sort_values(["datetime", "mtime"]).drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime")
    return df[["datetime", "open", "high", "low", "close"]]


def get_all_orders_from_logs(session: str) -> List[Dict]:
    return parse_orders_from_logs(session)


def get_all_insights_from_logs(session: str) -> List[Dict]:
    return parse_insights_from_logs(session)


def compute_drawdown(equity_df: pd.DataFrame) -> pd.DataFrame:
    if equity_df.empty:
        return pd.DataFrame()
    df = equity_df.copy()
    df["peak"] = df["close"].cummax()
    df["drawdown"] = (df["close"] - df["peak"]) / df["peak"] * 100
    return df[["datetime", "drawdown", "close", "peak"]]


def compute_period_returns(equity_df: pd.DataFrame, period_days: int = 7) -> pd.DataFrame:
    if equity_df.empty or "datetime" not in equity_df.columns:
        return pd.DataFrame()

    df = equity_df.copy().set_index("datetime").sort_index()
    daily_df = df["close"].resample("D").last().dropna().to_frame()
    if daily_df.empty and not df.empty:
        daily_df = df["close"].iloc[[-1]].to_frame()

    start_price = df["close"].iloc[0]
    start_date = df.index[0]
    rolling_returns = []

    for dt, row in daily_df.iterrows():
        curr_price = row["close"]
        lookback_dt = dt - pd.Timedelta(days=period_days)
        if lookback_dt < start_date:
            ref_price = start_price
        else:
            try:
                ref_price = df["close"].asof(lookback_dt)
                if pd.isna(ref_price):
                    ref_price = start_price
            except Exception:
                ref_price = start_price
        ret = (curr_price - ref_price) / ref_price * 100 if ref_price else 0.0
        rolling_returns.append(ret)

    daily_df["period_return"] = rolling_returns
    daily_df["cum_return"] = (daily_df["close"] / start_price - 1) * 100
    return daily_df.reset_index()


class SessionDataManager:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_path = LIVE_PATH / session_id
        self.storage_file = self.session_path / "processed_data.json"

    def load_stored_data(self) -> pd.DataFrame:
        if not self.storage_file.exists():
            return pd.DataFrame()
        try:
            data = json.loads(self.storage_file.read_text())
            df = pd.DataFrame(data)
            if not df.empty and "datetime" in df.columns:
                df["datetime"] = pd.to_datetime(df["datetime"])
            return df
        except Exception:
            return pd.DataFrame()

    def save_data(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        save_df = df.copy()
        if "datetime" in save_df.columns:
            save_df["datetime"] = save_df["datetime"].astype(str)
        try:
            self.storage_file.write_text(json.dumps(save_df.to_dict("records")))
        except Exception:
            pass

    def update(self, results: Dict) -> pd.DataFrame:
        stored = self.load_stored_data()
        runtime = results.get("runtimeStatistics", {}) if isinstance(results, dict) else {}

        def parse_dollar(s):
            try:
                return float(str(s).replace("$", "").replace(",", ""))
            except Exception:
                return 0.0

        sample = {
            "datetime": datetime.now(),
            "equity": parse_dollar(runtime.get("Equity", "0")),
            "holdings": parse_dollar(runtime.get("Holdings", "0")),
            "unrealized": parse_dollar(runtime.get("Unrealized", "0")),
            "fees": parse_dollar(runtime.get("Fees", "0")),
        }

        if sample["equity"] <= 0:
            return stored

        new_row = pd.DataFrame([sample])
        df = pd.concat([stored, new_row], ignore_index=True)
        df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime")
        self.save_data(df)
        return df
