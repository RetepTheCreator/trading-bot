"""
Portfolio-level risk manager.
Enforces all PDF risk rules and Rule 3 / Rule 5 from the user.
"""
from __future__ import annotations
from typing import Optional

from capital_manager import CapitalManager
from config import TradingConfig
from strategies.base import TradeSetup


class RiskManager:
    def __init__(self, config: TradingConfig, capital: CapitalManager):
        self.cfg = config
        self.capital = capital
        self._gap_cache: dict[str, float] = {}  # symbol → pre-market gap %

    # ── NEWS / GAP CACHE ──────────────────────────────────────────────────────
    def set_gap(self, symbol: str, gap_pct: float):
        self._gap_cache[symbol] = gap_pct

    def get_gap(self, symbol: str) -> float:
        return self._gap_cache.get(symbol, 0.0)

    def reset_daily(self):
        self._gap_cache.clear()

    # ── PORTFOLIO-LEVEL CHECKS ────────────────────────────────────────────────
    def total_open_risk_czk(self) -> float:
        """Sum of risk amounts of all open trades."""
        total = 0.0
        for trade in self.capital.open_trades.values():
            total += trade.risk_czk
        return total

    def daily_loss_pct(self) -> float:
        return abs(self.capital.daily_realized_pnl_czk) / self.capital.equity_czk \
            if self.capital.daily_realized_pnl_czk < 0 else 0.0

    def global_daily_limit_reached(self) -> bool:
        """Global 2% daily drawdown limit across all strategies."""
        return self.daily_loss_pct() >= 0.02

    # ── TRADE VALIDATION ─────────────────────────────────────────────────────
    def validate_setup(self, setup: TradeSetup) -> tuple[bool, str]:
        """
        Returns (ok, rejection_reason).
        Checks Rule 3 (capital limits) and Rule 5 (R:R ratio).
        """
        # Rule 3: cannot exceed available cash
        if setup.cost_czk > self.capital.cash_czk:
            return False, (
                f"Insufficient cash: need {setup.cost_czk:.0f} CZK, "
                f"available {self.capital.cash_czk:.0f} CZK"
            )

        # Rule 5: reward:risk must be >= 1.5 (60/40 profit:risk minimum)
        if setup.reward_risk_ratio < self.cfg.min_reward_risk_ratio:
            return False, (
                f"R:R {setup.reward_risk_ratio:.2f} below required "
                f"{self.cfg.min_reward_risk_ratio:.1f}"
            )

        # Sanity: stop must be on the correct side of entry
        if setup.direction == "LONG" and setup.stop_price >= setup.entry_price:
            return False, "Stop must be below entry for LONG"
        if setup.direction == "SHORT" and setup.stop_price <= setup.entry_price:
            return False, "Stop must be above entry for SHORT"

        # Capital protection: single trade risk should not exceed 1% of equity
        if setup.risk_czk > self.capital.equity_czk * 0.01:
            return False, (
                f"Single-trade risk {setup.risk_czk:.0f} CZK exceeds 1% of equity"
            )

        # Rule 3: equity must stay > 0 after worst case
        worst_case = self.capital.equity_czk - setup.risk_czk
        if worst_case <= 0:
            return False, "Trade would risk more equity than available"

        return True, ""

    # ── POSITION SIZE SUMMARY ─────────────────────────────────────────────────
    def sizing_summary(self, setup: TradeSetup) -> str:
        usd_risk = self.capital.czk_to_usd(setup.risk_czk)
        usd_cost = self.capital.czk_to_usd(setup.cost_czk)
        return (
            f"Shares: {setup.shares:.4f} | "
            f"Risk: {setup.risk_czk:.2f} CZK (${usd_risk:.2f}) | "
            f"Position value: {setup.cost_czk:.2f} CZK (${usd_cost:.2f}) | "
            f"R:R {setup.reward_risk_ratio:.2f}:1"
        )
