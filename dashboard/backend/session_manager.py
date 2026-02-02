"""
Session management functions for LEAN Live Trading Dashboard
Handles session-specific operations, actions, and Docker container management.
"""

import json
import subprocess
from pathlib import Path
from typing import Tuple, Optional, List, Dict

from .. import config
from .data_loader import get_session_path

COMMANDS_FOLDER = config.COMMANDS_FOLDER
SELL_ORDERS_FILE = config.SELL_ORDERS_FILE


def check_container_running(container_id: str) -> bool:
    if not container_id:
        return False
    try:
        result = subprocess.run(["docker", "ps", "-q", "-f", f"id={container_id}"], capture_output=True, text=True)
        return bool(result.stdout.strip())
    except Exception:
        return False


def terminate_container(container_id: str) -> Tuple[bool, str]:
    if not container_id:
        return False, "No container id"
    try:
        result = subprocess.run(["docker", "stop", container_id], capture_output=True, text=True)
        if result.returncode == 0:
            return True, "Stopped"
        return False, result.stderr.strip() or "Failed"
    except Exception as exc:
        return False, str(exc)


def write_sell_order(session: str, symbol: str, quantity: Optional[int] = None, limit_price: Optional[float] = None) -> bool:
    session_path = get_session_path(session)
    commands_dir = session_path / COMMANDS_FOLDER
    commands_dir.mkdir(parents=True, exist_ok=True)
    order_path = commands_dir / SELL_ORDERS_FILE
    order = {
        "symbol": symbol,
        "quantity": quantity,
        "limit_price": limit_price,
    }
    try:
        orders = []
        if order_path.exists():
            orders = json.loads(order_path.read_text())
        orders.append(order)
        order_path.write_text(json.dumps(orders))
        return True
    except Exception:
        return False


def get_pending_sell_orders(session: str) -> List[Dict]:
    order_path = get_session_path(session) / COMMANDS_FOLDER / SELL_ORDERS_FILE
    if not order_path.exists():
        return []
    try:
        data = json.loads(order_path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def clear_pending_sell_orders(session: str) -> None:
    order_path = get_session_path(session) / COMMANDS_FOLDER / SELL_ORDERS_FILE
    try:
        if order_path.exists():
            order_path.unlink()
    except Exception:
        pass


def get_session_status(session: str) -> Dict:
    status = {"session": session, "running": False}
    config_path = get_session_path(session) / "config"
    if not config_path.exists():
        return status
    try:
        cfg = json.loads(config_path.read_text())
        container_id = cfg.get("container", "")
        status["container"] = container_id
        status["running"] = check_container_running(container_id)
    except Exception:
        pass
    return status


def list_all_sessions_status() -> List[Dict]:
    sessions = []
    for session_dir in (config.LIVE_PATH.iterdir() if config.LIVE_PATH.exists() else []):
        if session_dir.is_dir():
            sessions.append(get_session_status(session_dir.name))
    return sessions
