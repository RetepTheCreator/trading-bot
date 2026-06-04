"""
Base strategy class and TradeSetup dataclass.
All strategy implementations inherit from BaseStrategy.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from config import TradingConfig


@dataclass
class TradeSetup:
    # Identity
    symbol: str
    strategy: str        # "MOMENTUM" | "BREAKOUT" | "MEAN_REVERSION"
    direction: str       # "LONG" | "SHORT"

    # Prices
    entry_price: float
    stop_price: float
    target_price: float
    partial_target: float    # Usually at 1R

    # Risk metrics
    risk_per_share: float    # abs(entry - stop)
    reward_per_share: float  # abs(target - entry)
    reward_risk_ratio: float # reward / risk  (must be >= config.min_reward_risk_ratio)

    # Sizing
    shares: float
    risk_czk: float
    cost_czk: float

    # Indicator context (for display and logging)
    vwap: float
    ema9: float
    ema20: float
    atr: float
    rvol_val: float
    rsi2: float

    reasoning: str
    timestamp: datetime


class BaseStrategy(ABC):
    name: str = "BASE"

    def __init__(self, config: TradingConfig, risk_manager):
        self.cfg = config
        self.rm = risk_manager
        self._daily_losses_czk: float = 0.0
        self._daily_losing_trades: int = 0
        self._daily_failed_breakouts: int = 0

    def reset_daily(self):
        self._daily_losses_czk = 0.0
        self._daily_losing_trades = 0
        self._daily_failed_breakouts = 0

    def record_loss(self, loss_czk: float):
        self._daily_losses_czk += abs(loss_czk)
        self._daily_losing_trades += 1

    def can_trade(self) -> tuple[bool, str]:
        """Check strategy-level daily limits before scanning."""
        return True, ""

    @abstractmethod
    def scan(
        self,
        symbol: str,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
    ) -> Optional[TradeSetup]:
        """Return a TradeSetup if conditions are met, else None."""

    @abstractmethod
    def manage_position(
        self,
        trade,
        current_price: float,
        df_1m: pd.DataFrame,
    ) -> tuple[str, float]:
        """
        Return (action, new_stop_or_exit_price).
        Actions: "HOLD" | "STOP_HIT" | "TARGET_HIT" | "MOVE_BE" |
                 "TRAIL" | "EXIT_SIGNAL" | "TIME_STOP"
        """
