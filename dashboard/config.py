"""
Configuration constants for LEAN Live Trading Dashboard
"""

from pathlib import Path

BASE_PATH = Path(__file__).parent.parent
LIVE_PATH = BASE_PATH / "live"
CACHE_PATH = LIVE_PATH / "_cache"
OBJECT_STORE_PATH = BASE_PATH / "storage"
EXAMPLE_DATA_DIR = Path(__file__).parent / "example_data"
COMMANDS_FOLDER = "commands"
SELL_ORDERS_FILE = "sell_orders.json"
EQUITY_CACHE_FILE = "equity_cache.json"
FEATURE_STORE_DIR = "features"

ORDER_STATUS = {
    0: "New",
    1: "Submitted",
    2: "PartiallyFilled",
    3: "Filled",
    4: "Canceled",
    5: "Invalid",
    6: "UpdateSubmitted",
}

ORDER_DIRECTION = {
    0: "Buy",
    1: "Sell",
    2: "Hold",
}

PAGE_CONFIG = {
    "page_title": "LEAN Live Trading Dashboard",
    "page_icon": "??",
    "layout": "wide",
    "initial_sidebar_state": "collapsed",
}

CHART_HEIGHTS = {
    "equity": 350,
    "benchmark": 450,
    "margin": 450,
    "aligned": 500,
}

DEFAULT_CACHE_MAX_POINTS = 10000
DEFAULT_LOG_LINES = 100
DEFAULT_REFRESH_RATE = 10
DEFAULT_EXAMPLE_MODE = True

COLORS = {
    "positive": "#00d4aa",
    "negative": "#ff4444",
    "neutral": "#cccccc",
    "warning": "#ffaa00",
    "info": "#6aa5ff",
    "background": "#1e1e1e",
    "surface": "#252526",
    "text": "#cccccc",
    "text_secondary": "#888",
}
