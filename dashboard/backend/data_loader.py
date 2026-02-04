"""
Data loading functions for LEAN Live Trading Dashboard
Handles loading of configs, results, orders, logs, and other data sources.
"""

import json
import time
import re
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import pandas as pd

from . import models
from .. import config

BASE_PATH = config.BASE_PATH
LIVE_PATH = config.LIVE_PATH
CACHE_PATH = config.CACHE_PATH
OBJECT_STORE_PATH = config.OBJECT_STORE_PATH
FEATURE_STORE_DIR = config.FEATURE_STORE_DIR
EXAMPLE_DATA_DIR = config.EXAMPLE_DATA_DIR
EQUITY_DATA_DIR = config.EQUITY_DATA_DIR
COMMANDS_FOLDER = config.COMMANDS_FOLDER
SELL_ORDERS_FILE = config.SELL_ORDERS_FILE
EQUITY_CACHE_FILE = config.EQUITY_CACHE_FILE
EXAMPLE_TICKERS = config.EXAMPLE_TICKERS
EXAMPLE_START_DATE = config.EXAMPLE_START_DATE
EXAMPLE_END_DATE = config.EXAMPLE_END_DATE
EXAMPLE_BASE_CAPITAL = config.EXAMPLE_BASE_CAPITAL
EXAMPLE_PRICE_SCALE = config.EXAMPLE_PRICE_SCALE

SESSION_ID_SEPARATOR = "::"
DEFAULT_ROOT_NAME = "root"
STRATEGY_ROOT_PREFIX = "S_"
ROOT_SESSION_NAME = "__root__"
_EXAMPLE_BUNDLE_CACHE: Optional[Dict] = None


def list_strategy_roots() -> List[str]:
    if not LIVE_PATH.exists():
        return []
    roots = []
    for entry in LIVE_PATH.iterdir():
        if entry.is_dir() and entry.name.startswith(STRATEGY_ROOT_PREFIX):
            roots.append(entry.name)
    return sorted(roots)


def get_strategy_root_map() -> Dict[str, List[str]]:
    root_map: Dict[str, List[str]] = {}
    if not LIVE_PATH.exists():
        return root_map
    for entry in LIVE_PATH.iterdir():
        if not entry.is_dir():
            continue
        root_map.setdefault(DEFAULT_ROOT_NAME, []).append(entry.name)
    for root in list_strategy_roots():
        root_path = LIVE_PATH / root
        sessions = [d.name for d in root_path.iterdir() if d.is_dir()]
        root_map[root] = sorted(sessions, reverse=True)
    return root_map


def make_session_id(root_name: str, session_name: str) -> str:
    if not root_name or root_name == DEFAULT_ROOT_NAME:
        return session_name
    return f"{root_name}{SESSION_ID_SEPARATOR}{session_name}"


def parse_session_id(session_id: str) -> Tuple[str, str]:
    if SESSION_ID_SEPARATOR in session_id:
        root, session = session_id.split(SESSION_ID_SEPARATOR, 1)
        return root, session
    return DEFAULT_ROOT_NAME, session_id


def get_session_path(session_id: str) -> Path:
    root, session = parse_session_id(session_id)
    if root == DEFAULT_ROOT_NAME or root == ROOT_SESSION_NAME:
        return LIVE_PATH / session
    return LIVE_PATH / root / session


def get_feature_store_root(session_id: str) -> Path:
    root, session = parse_session_id(session_id)
    if root == DEFAULT_ROOT_NAME or root == ROOT_SESSION_NAME:
        return LIVE_PATH / session / FEATURE_STORE_DIR
    return LIVE_PATH / root / session / FEATURE_STORE_DIR


def load_feature_factor_cache(session_id: str) -> Dict:
    path = get_feature_store_root(session_id) / "factor_cache.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def load_feature_intraday_cache(session_id: str) -> Dict:
    path = get_feature_store_root(session_id) / "intraday_cache.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def load_cmaes_meta(session_id: str) -> Dict:
    path = get_feature_store_root(session_id) / "cmaes_meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def get_live_sessions() -> List[str]:
    if not LIVE_PATH.exists():
        return []
    sessions = [d.name for d in LIVE_PATH.iterdir() if d.is_dir() and not d.name.startswith("_")]
    return sorted(sessions, reverse=True)


def parse_algorithm_class_name(config_data: Dict) -> Optional[str]:
    if not isinstance(config_data, dict):
        return None
    return config_data.get("algorithm-type-name") or config_data.get("algorithm")


def find_module_name_from_log(log_content: str) -> Optional[str]:
    if not log_content:
        return None
    match = re.search(r"Algorithm.*?Name:\s*([\w\.]+)", log_content)
    if match:
        return match.group(1)
    return None


def get_session_strategy(session_id: str) -> Optional[str]:
    config_data = load_config(session_id)
    if config_data:
        return parse_algorithm_class_name(config_data)
    log_tail = load_log_tail(session_id, lines=200)
    return find_module_name_from_log(log_tail)


def build_session_catalog() -> List[Dict]:
    catalog = []
    for session in get_live_sessions():
        catalog.append({
            "session": session,
            "strategy": get_session_strategy(session) or "",
        })
    return catalog


def load_config(session: str) -> Optional[Dict]:
    config_path = get_session_path(session) / "config"
    if not config_path.exists():
        return None
    try:
        return json.loads(config_path.read_text())
    except Exception:
        return None


def get_main_results_file(session: str) -> Optional[Path]:
    session_path = get_session_path(session)
    config_data = load_config(session)
    if config_data and "id" in config_data:
        results_file = session_path / f"L-{config_data['id']}.json"
        if results_file.exists():
            return results_file
    for f in session_path.glob("L-*.json"):
        if not any(x in f.name for x in ["minute", "second", "order-events"]):
            return f
    return None


def load_results(session: str) -> Optional[Dict]:
    results_file = get_main_results_file(session)
    if not results_file:
        return None
    for attempt in range(3):
        try:
            return json.loads(results_file.read_text())
        except Exception:
            if attempt < 2:
                time.sleep(0.1)
    return None


def load_order_events(session: str) -> List[Dict]:
    session_path = get_session_path(session)
    events: List[Dict] = []
    for f in session_path.glob("L-*-order-events.json"):
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                events.extend(data)
        except Exception:
            continue
    events.sort(key=lambda x: x.get("time", 0), reverse=True)
    return events


def load_alpha_insights(session: str) -> List[Dict]:
    session_path = get_session_path(session)
    config_data = load_config(session)
    if not config_data:
        return []
    alpha_path = session_path / f"L-{config_data['id']}" / "alpha-results.json"
    if not alpha_path.exists():
        return []
    try:
        return json.loads(alpha_path.read_text())
    except Exception:
        return []


def parse_orders_from_logs(session: str) -> List[Dict]:
    return []


def parse_insights_from_logs(session: str) -> List[Dict]:
    return []


def parse_lean_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def extract_equity_series(data: Dict) -> List[Dict]:
    charts = data.get("charts", {}) if isinstance(data, dict) else {}
    strategy = charts.get("Strategy Equity", {}) if isinstance(charts, dict) else {}
    series = strategy.get("series", {}) if isinstance(strategy, dict) else {}
    equity_series = series.get("Equity", {}) if isinstance(series, dict) else {}
    values = equity_series.get("values", []) if isinstance(equity_series, dict) else []

    points = []
    for entry in values:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        ts = entry[0]
        if not isinstance(ts, (int, float)):
            continue
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        try:
            dt = datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            continue
        try:
            if len(entry) >= 5:
                open_v = float(entry[1])
                high_v = float(entry[2])
                low_v = float(entry[3])
                close_v = float(entry[4])
            else:
                close_v = float(entry[1])
                open_v = close_v
                high_v = close_v
                low_v = close_v
        except (TypeError, ValueError):
            continue
        points.append({
            "datetime": dt,
            "open": open_v,
            "high": high_v,
            "low": low_v,
            "close": close_v,
        })
    return points


def load_equity_cache(session: str) -> List[Dict]:
    session_path = get_session_path(session)
    cache_path = session_path / EQUITY_CACHE_FILE
    if not cache_path.exists():
        return []
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return []


def load_log_tail(session: str, lines: int = 100) -> str:
    log_path = get_session_path(session) / "log.txt"
    if not log_path.exists():
        return "No log file found."
    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        filtered_lines = [
            line for line in all_lines
            if "Isolator.ExecuteWithTimeLimit()" not in line
            and "LiveMappingEventProvider(" not in line
            and "[Benchmark]" not in line
            and ("[Margin]" not in line and "Margin Used" not in line and "Margin Remaining" not in line)
            and "BrokerageTransactionHandler.Process()" not in line
            and "Total margin information:" not in line
            and "Order request margin information:" not in line
            and "LiveTradingResultHandler.OrderEvent()" not in line
            and "BacktestingBrokerage.PlaceOrder()" not in line
            and "Debug: New Order Event:" not in line
        ]
        return "\n".join(filtered_lines[-lines:])
    except Exception:
        return "Error reading log file."


def extract_recent_errors(session: str, max_lines: int = 200) -> List[str]:
    log_path = get_session_path(session) / "log.txt"
    if not log_path.exists():
        return []
    errors = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines[-max_lines:]:
            if "ERROR" in line or "Exception" in line:
                errors.append(line)
    except Exception:
        pass
    return errors


def parse_server_stats(session: str) -> Dict:
    log_path = get_session_path(session) / "log.txt"
    stats = {"cpu": 0, "ram_used": 0, "ram_total": 0, "uptime": "0d 00:00:00"}

    if not log_path.exists():
        return stats

    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        isolator_lines = [line for line in all_lines if "Isolator.ExecuteWithTimeLimit()" in line]

        if isolator_lines:
            latest = isolator_lines[-1]
            used_match = re.search(r"Used:\s*(\d+)", latest)
            app_match = re.search(r"App:\s*(\d+)", latest)
            cpu_match = re.search(r"CPU:\s*(\d+)%", latest)
            if used_match:
                stats["ram_used"] = int(used_match.group(1))
            if app_match:
                stats["ram_total"] = int(app_match.group(1))
            if cpu_match:
                stats["cpu"] = int(cpu_match.group(1))

        if all_lines:
            first_time = None
            last_time = None
            for line in all_lines[:10]:
                match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
                if match:
                    first_time = datetime.fromisoformat(match.group(1))
                    break
            for line in reversed(all_lines[-10:]):
                match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
                if match:
                    last_time = datetime.fromisoformat(match.group(1))
                    break
            if first_time and last_time:
                delta = last_time - first_time
                days = delta.days
                hours, remainder = divmod(delta.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                stats["uptime"] = f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    except Exception:
        pass

    return stats


def parse_margin_from_logs(session: str) -> List[Dict]:
    log_path = get_session_path(session) / "log.txt"
    margin_data: List[Dict] = []
    if not log_path.exists():
        return margin_data
    try:
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "Margin" in line and ("Used" in line or "Remaining" in line):
                match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
                if match:
                    timestamp = datetime.fromisoformat(match.group(1))
                    used_match = re.search(r"Used[:\s]+\$?([\d,]+\.?\d*)", line)
                    remaining_match = re.search(r"Remaining[:\s]+\$?([\d,]+\.?\d*)", line)
                    if used_match or remaining_match:
                        margin_data.append({
                            "datetime": timestamp,
                            "margin_used": float(used_match.group(1).replace(",", "")) if used_match else 0,
                            "margin_remaining": float(remaining_match.group(1).replace(",", "")) if remaining_match else 0,
                        })
    except Exception:
        pass
    return margin_data


def fetch_benchmark_data(session: str):
    session_path = get_session_path(session)
    spy_data = []
    json_path = session_path / "benchmark_spy.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text())
            for item in data:
                spy_data.append({
                    "datetime": datetime.fromtimestamp(item["timestamp"]),
                    "close": item["price"],
                })
            if spy_data:
                import pandas as pd
                df = pd.DataFrame(spy_data)
                return df.drop_duplicates(subset=["datetime"]).sort_values("datetime")
        except Exception:
            pass

    log_path = session_path / "log.txt"
    if not log_path.exists():
        import pandas as pd
        return pd.DataFrame()

    try:
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "[Benchmark]" in line and "SPY" in line:
                timestamp_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
                price_match = re.search(r"SPY close:\s*([\d.]+)", line)
                if timestamp_match and price_match:
                    try:
                        spy_data.append({
                            "datetime": datetime.fromisoformat(timestamp_match.group(1)),
                            "close": float(price_match.group(1)),
                        })
                    except Exception:
                        pass
        if spy_data:
            import pandas as pd
            df = pd.DataFrame(spy_data)
            return df.drop_duplicates(subset=["datetime"]).sort_values("datetime")
    except Exception:
        pass

    import pandas as pd
    return pd.DataFrame()


def load_example_json(name: str):
    path = EXAMPLE_DATA_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_example_date(date_value: str) -> Optional[datetime]:
    if not date_value or date_value.lower() == "latest":
        return None
    try:
        return datetime.fromisoformat(date_value)
    except ValueError:
        return None


def _resolve_equity_dir() -> Path:
    candidates = []
    if EQUITY_DATA_DIR:
        candidates.append(EQUITY_DATA_DIR)
    base = BASE_PATH
    for _ in range(4):
        candidate = base.parent / "data" / "equity" / "usa" / "daily"
        if candidate not in candidates:
            candidates.append(candidate)
        base = base.parent
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return EQUITY_DATA_DIR


def _read_equity_zip(ticker: str) -> Optional[pd.DataFrame]:
    equity_dir = _resolve_equity_dir()
    zip_path = equity_dir / f"{ticker.lower()}.zip"
    if not zip_path.exists():
        return None
    try:
        with zipfile.ZipFile(zip_path) as handle:
            names = [name for name in handle.namelist() if name.lower().endswith(".csv")]
            if not names:
                return None
            with handle.open(names[0]) as csv_handle:
                df = pd.read_csv(
                    csv_handle,
                    header=None,
                    names=["datetime", "open", "high", "low", "close", "volume"],
                )
    except Exception:
        return None

    df["datetime"] = pd.to_datetime(
        df["datetime"].astype(str).str.strip(),
        format="%Y%m%d %H:%M",
        errors="coerce",
    )
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce") / float(EXAMPLE_PRICE_SCALE)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    df = df.dropna(subset=["datetime", "close"]).sort_values("datetime")
    if df.empty:
        return None
    return df


def build_example_portfolio_bundle() -> Optional[Dict]:
    tickers = list(EXAMPLE_TICKERS or [])
    if not tickers:
        return None

    start_dt = _parse_example_date(EXAMPLE_START_DATE)
    end_dt = _parse_example_date(EXAMPLE_END_DATE)

    frames: Dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        df = _read_equity_zip(ticker)
        if df is None or df.empty:
            return None
        if start_dt is not None:
            df = df[df["datetime"] >= start_dt]
        if end_dt is not None:
            df = df[df["datetime"] <= end_dt]
        df = df.dropna(subset=["datetime", "close"]).sort_values("datetime")
        if df.empty:
            return None
        frames[ticker] = df.set_index("datetime")

    common_index = None
    for df in frames.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    if common_index is None or common_index.empty:
        return None
    common_index = common_index.sort_values()

    allocation_dt = common_index[0]
    per_ticker = float(EXAMPLE_BASE_CAPITAL) / len(tickers)
    shares: Dict[str, float] = {}
    for ticker, df in frames.items():
        start_close = float(df.loc[allocation_dt, "close"])
        if start_close <= 0:
            return None
        shares[ticker] = per_ticker / start_close

    portfolio = pd.DataFrame(index=common_index)
    for field in ["open", "high", "low", "close"]:
        total = None
        for ticker, df in frames.items():
            series = df.loc[common_index, field].astype(float) * shares[ticker]
            total = series if total is None else total.add(series, fill_value=0)
        portfolio[field] = total

    equity = [
        {
            "datetime": idx.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for idx, row in portfolio.iterrows()
    ]

    last_dt = common_index[-1]
    positions = []
    holdings_total = 0.0
    unrealized_total = 0.0
    initial_invested = 0.0
    for ticker, df in frames.items():
        avg = float(df.loc[allocation_dt, "close"])
        cur = float(df.loc[last_dt, "close"])
        qty = float(shares[ticker])
        value = qty * cur
        unrealized = (cur - avg) * qty
        holdings_total += value
        unrealized_total += unrealized
        initial_invested += qty * avg
        pnl_pct = (cur / avg - 1) * 100 if avg else 0.0
        positions.append({
            "symbol": ticker,
            "q": qty,
            "a": avg,
            "p": cur,
            "v": value,
            "u": unrealized,
            "up": pnl_pct,
            "fx": 1,
        })

    cash = float(EXAMPLE_BASE_CAPITAL) - initial_invested
    equity_value = holdings_total + cash
    net_profit = equity_value - float(EXAMPLE_BASE_CAPITAL)
    return_pct = (net_profit / float(EXAMPLE_BASE_CAPITAL) * 100) if EXAMPLE_BASE_CAPITAL else 0.0

    runtime_stats = {
        "Equity": f"${equity_value:,.2f}",
        "Fees": "$0.00",
        "Holdings": f"${holdings_total:,.2f}",
        "Net Profit": f"{'+' if net_profit >= 0 else '-'}${abs(net_profit):,.2f}",
        "Probabilistic Sharpe Ratio": "0%",
        "Return": f"{return_pct:+.2f} %",
        "Unrealized": f"{'+' if unrealized_total >= 0 else '-'}${abs(unrealized_total):,.2f}",
        "Volume": "$0",
    }

    account = {
        "account_currency": "USD",
        "cash": cash,
        "holdings": holdings_total,
        "equity": equity_value,
        "runtimeStatistics": runtime_stats,
    }

    benchmark = []
    spy_df = _read_equity_zip("SPY")
    if spy_df is not None and not spy_df.empty:
        spy_df = spy_df.set_index("datetime").reindex(common_index).dropna(subset=["close"])
        benchmark = [
            {"datetime": idx.isoformat(), "close": float(row["close"])}
            for idx, row in spy_df.iterrows()
        ]

    return {
        "equity": equity,
        "positions": positions,
        "account": account,
        "benchmarks": benchmark,
    }


def _get_example_bundle() -> Dict:
    global _EXAMPLE_BUNDLE_CACHE
    if _EXAMPLE_BUNDLE_CACHE is not None:
        return _EXAMPLE_BUNDLE_CACHE
    bundle = build_example_portfolio_bundle()
    _EXAMPLE_BUNDLE_CACHE = bundle or {}
    return _EXAMPLE_BUNDLE_CACHE


def load_example_account() -> Dict:
    bundle = _get_example_bundle()
    if "account" in bundle:
        return bundle["account"]
    data = load_example_json("account.json")
    return data if isinstance(data, dict) else {}


def load_example_positions() -> List[Dict]:
    bundle = _get_example_bundle()
    if "positions" in bundle:
        return bundle["positions"]
    data = load_example_json("positions.json")
    return data if isinstance(data, list) else []


def load_example_equity() -> List[Dict]:
    bundle = _get_example_bundle()
    if "equity" in bundle:
        return bundle["equity"]
    data = load_example_json("equity.json")
    return data if isinstance(data, list) else []


def load_example_benchmarks() -> List[Dict]:
    bundle = _get_example_bundle()
    if "benchmarks" in bundle:
        return bundle["benchmarks"]
    data = load_example_json("benchmarks.json")
    return data if isinstance(data, list) else []


def load_example_orders() -> List[Dict]:
    data = load_example_json("orders.json")
    return data if isinstance(data, list) else []


def load_example_insights() -> List[Dict]:
    data = load_example_json("insights.json")
    return data if isinstance(data, list) else []


def load_example_logs() -> List[str]:
    data = load_example_json("logs.json")
    return data if isinstance(data, list) else []


def load_example_server_stats() -> Dict:
    data = load_example_json("server_stats.json")
    return data if isinstance(data, dict) else {}
