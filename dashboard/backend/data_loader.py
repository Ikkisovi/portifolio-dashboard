"""
Data loading functions for LEAN Live Trading Dashboard
Handles loading of configs, results, orders, logs, and other data sources.
"""

import json
import time
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple

from . import models
from .. import config

BASE_PATH = config.BASE_PATH
LIVE_PATH = config.LIVE_PATH
CACHE_PATH = config.CACHE_PATH
OBJECT_STORE_PATH = config.OBJECT_STORE_PATH
FEATURE_STORE_DIR = config.FEATURE_STORE_DIR
EXAMPLE_DATA_DIR = config.EXAMPLE_DATA_DIR
COMMANDS_FOLDER = config.COMMANDS_FOLDER
SELL_ORDERS_FILE = config.SELL_ORDERS_FILE
EQUITY_CACHE_FILE = config.EQUITY_CACHE_FILE

SESSION_ID_SEPARATOR = "::"
DEFAULT_ROOT_NAME = "root"
STRATEGY_ROOT_PREFIX = "S_"
ROOT_SESSION_NAME = "__root__"


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


def load_example_account() -> Dict:
    data = load_example_json("account.json")
    return data if isinstance(data, dict) else {}


def load_example_positions() -> List[Dict]:
    data = load_example_json("positions.json")
    return data if isinstance(data, list) else []


def load_example_equity() -> List[Dict]:
    data = load_example_json("equity.json")
    return data if isinstance(data, list) else []


def load_example_benchmarks() -> List[Dict]:
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
