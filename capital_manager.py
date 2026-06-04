"""
Capital tracking – Rule 3: bot operates ONLY within its allocated 2000 CZK.
All values stored in CZK; USD positions converted at current FX rate.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Optional

from config import TradingConfig


@dataclass
class Trade:
    trade_id: str
    symbol: str
    strategy: str
    direction: str       # "LONG" | "SHORT"
    entry_price: float
    shares: float
    stop_price: float
    target_price: float
    partial_target: float
    risk_czk: float
    timestamp: str

    status: str = "OPEN"   # OPEN | PARTIALLY_CLOSED | CLOSED
    current_stop: float = 0.0
    partial_done: bool = False
    stop_at_be: bool = False

    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl_czk: float = 0.0
    exit_reason: str = ""


class CapitalManager:
    def __init__(self, config: TradingConfig):
        self.config = config
        self._state_file = os.path.join(config.log_dir, "capital_state.json")
        os.makedirs(config.log_dir, exist_ok=True)
        self._load()

    def _load(self):
        if os.path.exists(self._state_file):
            with open(self._state_file) as f:
                s = json.load(f)
            self.equity_czk: float = s["equity_czk"]
            self.cash_czk: float = s["cash_czk"]
            self.open_trades: Dict[str, Trade] = {
                k: Trade(**v) for k, v in s.get("open_trades", {}).items()
            }
            self.closed_trades: list = s.get("closed_trades", [])
            self.daily_realized_pnl_czk: float = s.get("daily_pnl", 0.0)
        else:
            self.equity_czk = self.config.starting_capital_czk
            self.cash_czk = self.config.starting_capital_czk
            self.open_trades: Dict[str, Trade] = {}
            self.closed_trades: list = []
            self.daily_realized_pnl_czk: float = 0.0
            self._save()

    def _save(self):
        state = {
            "equity_czk": self.equity_czk,
            "cash_czk": self.cash_czk,
            "open_trades": {k: asdict(v) for k, v in self.open_trades.items()},
            "closed_trades": self.closed_trades,
            "daily_pnl": self.daily_realized_pnl_czk,
        }
        with open(self._state_file, "w") as f:
            json.dump(state, f, indent=2)

    # ── FX ────────────────────────────────────────────────────────────────────
    def usd_to_czk(self, usd: float) -> float:
        return usd * self.config.czk_usd_rate

    def czk_to_usd(self, czk: float) -> float:
        return czk / self.config.czk_usd_rate

    # ── POSITION SIZING ───────────────────────────────────────────────────────
    def calc_position(
        self,
        risk_pct: float,
        stop_distance_usd: float,
        entry_price_usd: float,
    ) -> tuple[float, float, float]:
        """
        Returns (shares, risk_czk, cost_czk).
        PDF formula: position_size = account_risk_amount ÷ stop_distance
        Never risks more than allocated capital.
        """
        risk_czk = self.equity_czk * risk_pct
        risk_usd = self.czk_to_usd(risk_czk)

        if stop_distance_usd <= 0:
            return 0.0, 0.0, 0.0

        shares = risk_usd / stop_distance_usd

        # Enforce minimum trade size
        cost_usd = shares * entry_price_usd
        if cost_usd < self.config.min_trade_usd:
            shares = self.config.min_trade_usd / entry_price_usd
            cost_usd = self.config.min_trade_usd

        cost_czk = self.usd_to_czk(cost_usd)
        risk_czk = shares * stop_distance_usd * self.config.czk_usd_rate

        # Rule 3: never use more cash than available
        if cost_czk > self.cash_czk:
            shares = self.czk_to_usd(self.cash_czk) / entry_price_usd
            cost_czk = self.cash_czk
            risk_czk = shares * stop_distance_usd * self.config.czk_usd_rate

        return shares, risk_czk, cost_czk

    # ── TRADE LIFECYCLE ───────────────────────────────────────────────────────
    def open_trade(self, trade: Trade, cost_czk: float):
        self.cash_czk -= cost_czk
        self.open_trades[trade.trade_id] = trade
        trade.current_stop = trade.stop_price
        self._save()

    def partial_close(self, trade_id: str, exit_price_usd: float, shares_closed: float):
        trade = self.open_trades[trade_id]
        pnl_usd = (exit_price_usd - trade.entry_price) * shares_closed
        if trade.direction == "SHORT":
            pnl_usd = -pnl_usd
        pnl_czk = self.usd_to_czk(pnl_usd)

        proceeds_czk = self.usd_to_czk(exit_price_usd * shares_closed)
        cost_czk = self.usd_to_czk(trade.entry_price * shares_closed)
        self.cash_czk += cost_czk + pnl_czk

        trade.pnl_czk += pnl_czk
        trade.shares -= shares_closed
        trade.partial_done = True
        trade.status = "PARTIALLY_CLOSED"
        self.daily_realized_pnl_czk += pnl_czk
        self._save()
        return pnl_czk

    def close_trade(self, trade_id: str, exit_price_usd: float, reason: str):
        trade = self.open_trades.pop(trade_id)
        pnl_usd = (exit_price_usd - trade.entry_price) * trade.shares
        if trade.direction == "SHORT":
            pnl_usd = -pnl_usd
        pnl_czk = self.usd_to_czk(pnl_usd)

        proceeds_czk = self.usd_to_czk(exit_price_usd * trade.shares)
        cost_czk = self.usd_to_czk(trade.entry_price * trade.shares)
        self.cash_czk += cost_czk + pnl_czk

        trade.pnl_czk += pnl_czk
        trade.exit_price = exit_price_usd
        trade.exit_time = datetime.now().isoformat()
        trade.status = "CLOSED"
        trade.exit_reason = reason
        self.equity_czk += pnl_czk
        self.daily_realized_pnl_czk += pnl_czk

        # Rule 3: equity floor – can never go below 0
        self.equity_czk = max(self.equity_czk, 0.0)
        self.cash_czk = max(self.cash_czk, 0.0)

        self.closed_trades.append(asdict(trade))
        self._save()
        return pnl_czk

    def update_stop(self, trade_id: str, new_stop: float):
        if trade_id in self.open_trades:
            self.open_trades[trade_id].current_stop = new_stop
            self._save()

    def reset_daily(self):
        self.daily_realized_pnl_czk = 0.0
        self._save()

    # ── SNAPSHOT ──────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        unrealized = sum(
            t.pnl_czk for t in self.open_trades.values()
        )
        return {
            "equity_czk": round(self.equity_czk, 2),
            "cash_czk": round(self.cash_czk, 2),
            "open_positions": len(self.open_trades),
            "daily_realized_pnl_czk": round(self.daily_realized_pnl_czk, 2),
            "daily_pnl_pct": round(
                self.daily_realized_pnl_czk / self.config.starting_capital_czk * 100, 2
            ),
        }
