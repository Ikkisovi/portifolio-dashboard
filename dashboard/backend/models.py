"""
Data models for LEAN Live Trading Dashboard
Provides object-oriented access to account and position data.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class CashPosition:
    currency: str
    amount: float
    conversion_rate: float
    value_in_account: float


@dataclass
class HoldingPosition:
    symbol: str
    quantity: float
    average_price: float
    price: float
    value: float
    unrealized: float
    unrealized_pct: float
    fx_rate: float


@dataclass
class AccountSnapshot:
    account_currency: str
    cash_total: float
    invested: float
    equity: float
    unrealized: float
    cashbook: Dict[str, CashPosition] = field(default_factory=dict)
    positions: List[HoldingPosition] = field(default_factory=list)
