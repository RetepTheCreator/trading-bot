"""
Order execution layer.
  • "paper" mode  – simulates fills at current market price (default).
  • "alpaca" mode – places real orders via the Alpaca Trading API.

Hard stops are ALWAYS auto-executed (Rule 3: capital must be protected).
"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional

from capital_manager import CapitalManager, Trade
from config import TradingConfig
from strategies.base import TradeSetup


class OrderManager:
    def __init__(self, config: TradingConfig, capital: CapitalManager, notifier):
        self.cfg = config
        self.capital = capital
        self.notifier = notifier
        self._alpaca = None

        if config.broker == "alpaca":
            self._init_alpaca()

    def _init_alpaca(self):
        try:
            from alpaca.trading.client import TradingClient
            self._alpaca = TradingClient(
                self.cfg.alpaca_key,
                self.cfg.alpaca_secret,
                paper=self.cfg.alpaca_paper,
            )
            self.notifier.info(
                f"Alpaca {'paper' if self.cfg.alpaca_paper else 'LIVE'} mode connected."
            )
        except Exception as e:
            self.notifier.alert(f"Alpaca init failed: {e}. Falling back to paper mode.")
            self.cfg.broker = "paper"

    # ── OPEN POSITION ─────────────────────────────────────────────────────────
    def open_position(self, setup: TradeSetup) -> Optional[str]:
        """
        Execute entry order.  Returns trade_id on success, None on failure.
        """
        trade_id = str(uuid.uuid4())[:8]

        if self.cfg.broker == "paper":
            fill_price = setup.entry_price
        else:
            fill_price = self._alpaca_market_order(
                setup.symbol, setup.shares, setup.direction
            )
            if fill_price is None:
                return None

        trade = Trade(
            trade_id=trade_id,
            symbol=setup.symbol,
            strategy=setup.strategy,
            direction=setup.direction,
            entry_price=fill_price,
            shares=setup.shares,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            partial_target=setup.partial_target,
            risk_czk=setup.risk_czk,
            timestamp=datetime.now().isoformat(),
            current_stop=setup.stop_price,
        )

        cost_czk = self.capital.usd_to_czk(fill_price * setup.shares)
        self.capital.open_trade(trade, cost_czk)

        self.notifier.trade(
            f"OPENED {setup.direction} {setup.symbol} | "
            f"{setup.shares:.4f} shares @ ${fill_price:.2f} | "
            f"Stop ${setup.stop_price:.2f} | Target ${setup.target_price:.2f} | "
            f"R:R {setup.reward_risk_ratio:.2f}"
        )
        return trade_id

    # ── PARTIAL CLOSE ─────────────────────────────────────────────────────────
    def partial_close(self, trade_id: str, exit_price: float, pct: float) -> float:
        trade = self.capital.open_trades.get(trade_id)
        if not trade:
            return 0.0

        shares_to_close = round(trade.shares * pct, 4)
        if self.cfg.broker == "alpaca":
            fill = self._alpaca_close_partial(trade.symbol, shares_to_close, trade.direction)
            if fill is None:
                fill = exit_price
        else:
            fill = exit_price

        pnl_czk = self.capital.partial_close(trade_id, fill, shares_to_close)
        self.notifier.trade(
            f"PARTIAL EXIT {trade.symbol} | {shares_to_close:.4f} shares @ ${fill:.2f} | "
            f"P&L: {pnl_czk:+.2f} CZK"
        )
        return pnl_czk

    # ── FULL CLOSE ────────────────────────────────────────────────────────────
    def close_position(self, trade_id: str, exit_price: float, reason: str) -> float:
        trade = self.capital.open_trades.get(trade_id)
        if not trade:
            return 0.0

        if self.cfg.broker == "alpaca":
            fill = self._alpaca_close_all(trade.symbol, trade.direction)
            if fill is None:
                fill = exit_price
        else:
            fill = exit_price

        pnl_czk = self.capital.close_trade(trade_id, fill, reason)
        emoji = "✓" if pnl_czk >= 0 else "✗"
        self.notifier.trade(
            f"{emoji} CLOSED {trade.symbol} ({reason}) | "
            f"@ ${fill:.2f} | P&L: {pnl_czk:+.2f} CZK"
        )
        return pnl_czk

    # ── ALPACA HELPERS ────────────────────────────────────────────────────────
    def _alpaca_market_order(
        self, symbol: str, qty: float, direction: str
    ) -> Optional[float]:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = self._alpaca.submit_order(req)
            # Alpaca fills market orders near-instantly for paper
            return float(order.filled_avg_price or 0)
        except Exception as e:
            self.notifier.alert(f"Alpaca order error: {e}")
            return None

    def _alpaca_close_partial(
        self, symbol: str, qty: float, direction: str
    ) -> Optional[float]:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            side = OrderSide.SELL if direction == "LONG" else OrderSide.BUY
            req = MarketOrderRequest(
                symbol=symbol, qty=qty,
                side=side, time_in_force=TimeInForce.DAY
            )
            order = self._alpaca.submit_order(req)
            return float(order.filled_avg_price or 0)
        except Exception as e:
            self.notifier.alert(f"Alpaca partial close error: {e}")
            return None

    def _alpaca_close_all(self, symbol: str, direction: str) -> Optional[float]:
        try:
            pos = self._alpaca.get_open_position(symbol)
            return self._alpaca_close_partial(symbol, float(pos.qty), direction)
        except Exception as e:
            self.notifier.alert(f"Alpaca close-all error: {e}")
            return None
