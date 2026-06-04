"""
Intraday Momentum Strategy – implemented exactly per PDF pages 2-4.

Entry logic (LONG):
  1. Price above session VWAP
  2. 20 EMA sloping up
  3. RVOL > 2.0
  4. First valid pullback that holds 9 EMA or VWAP
  5. Entry on break of trigger bar high

Stop:  below pullback low or 0.75 ATR(14)
Target: 1.8R primary (partial at 1R)
Secondary exit: 9 EMA break, VWAP loss, time stop (3-5 bars no progress)

SHORT is the mirror image of all the above.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from config import TradingConfig
from indicators import add_all
from strategies.base import BaseStrategy, TradeSetup


class MomentumStrategy(BaseStrategy):
    name = "MOMENTUM"

    # ── DAILY LIMITS (PDF p.3) ────────────────────────────────────────────────
    def can_trade(self) -> tuple[bool, str]:
        cap = self.cfg.momentum_daily_loss_cap_pct * self.rm.capital.equity_czk
        if self._daily_losses_czk >= cap:
            return False, f"Daily loss cap reached ({self._daily_losses_czk:.0f} CZK)"
        if self._daily_losing_trades >= self.cfg.momentum_max_losing_trades:
            return False, f"Max losing trades ({self.cfg.momentum_max_losing_trades}) reached"
        return True, ""

    # ── SCAN ──────────────────────────────────────────────────────────────────
    def scan(
        self,
        symbol: str,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
    ) -> Optional[TradeSetup]:
        if len(df_1m) < 30:
            return None

        df = add_all(df_1m, self.cfg)
        row = df.iloc[-1]

        vwap = row["vwap"]
        ema9 = row["ema9"]
        ema20 = row["ema20"]
        atr_val = row["atr"]
        rvol_val = row["rvol"]
        price = row["close"]

        if any(pd.isna([vwap, ema9, ema20, atr_val, rvol_val])):
            return None

        # PDF: "require relative volume above 2.0"
        if rvol_val < self.cfg.min_rvol:
            return None

        direction = self._detect_direction(df)
        if direction is None:
            return None

        setup = self._build_setup(symbol, df, direction, atr_val, vwap, ema9, ema20, rvol_val, row["rsi2"])
        return setup

    def _detect_direction(self, df: pd.DataFrame) -> Optional[str]:
        """
        LONG conditions (PDF p.3):
          - price above VWAP
          - 20 EMA sloping up (higher now than 3 bars ago)
          - shallow pullback: last 2-4 bars declining but holding 9 EMA or VWAP
          - trigger: most recent close > prior bar high (momentum resuming)

        SHORT is the mirror image.
        """
        row = df.iloc[-1]
        prev = df.iloc[-2]
        recent = df.iloc[-5:]

        price = row["close"]
        vwap = row["vwap"]
        ema9 = row["ema9"]
        ema20 = row["ema20"]

        # ── LONG ─────────────────────────────────────────────────────────────
        ema20_up = df["ema20"].iloc[-1] > df["ema20"].iloc[-4]
        pullback_held = recent["low"].min() >= (ema9 * 0.999)  # held 9 EMA within 0.1%
        vwap_held = recent["low"].min() >= (vwap * 0.998)
        price_above_vwap = price > vwap
        trigger_long = price > prev["high"]  # break of prior bar high

        if (
            price_above_vwap
            and ema20_up
            and (pullback_held or vwap_held)
            and trigger_long
        ):
            return "LONG"

        # ── SHORT ─────────────────────────────────────────────────────────────
        ema20_down = df["ema20"].iloc[-1] < df["ema20"].iloc[-4]
        pullback_held_short = recent["high"].max() <= (ema9 * 1.001)
        vwap_held_short = recent["high"].max() <= (vwap * 1.002)
        price_below_vwap = price < vwap
        trigger_short = price < prev["low"]

        if (
            price_below_vwap
            and ema20_down
            and (pullback_held_short or vwap_held_short)
            and trigger_short
        ):
            return "SHORT"

        return None

    def _build_setup(
        self, symbol, df, direction, atr_val, vwap, ema9, ema20, rvol_val, rsi2
    ) -> Optional[TradeSetup]:
        row = df.iloc[-1]
        prev = df.iloc[-2]
        recent = df.iloc[-5:]

        price = row["close"]

        if direction == "LONG":
            entry = prev["high"] + 0.01   # break of trigger bar high
            pullback_low = recent["low"].min()
            stop = min(pullback_low, entry - self.cfg.momentum_stop_atr_mult * atr_val) - 0.01
            target = entry + self.cfg.momentum_target_r * (entry - stop)
            partial = entry + 1.0 * (entry - stop)  # 1R partial target
        else:
            entry = prev["low"] - 0.01
            pullback_high = recent["high"].max()
            stop = max(pullback_high, entry + self.cfg.momentum_stop_atr_mult * atr_val) + 0.01
            target = entry - self.cfg.momentum_target_r * (stop - entry)
            partial = entry - 1.0 * (stop - entry)

        risk_per_share = abs(entry - stop)
        reward_per_share = abs(target - entry)
        if risk_per_share <= 0:
            return None

        rr = reward_per_share / risk_per_share
        if rr < self.cfg.min_reward_risk_ratio:
            return None  # Rule 5: profit:risk must be >= 1.5

        shares, risk_czk, cost_czk = self.rm.capital.calc_position(
            self.cfg.momentum_risk_pct, risk_per_share, entry
        )
        if shares <= 0:
            return None

        reason = (
            f"Momentum {direction}: price {'above' if direction=='LONG' else 'below'} VWAP "
            f"({vwap:.2f}), 20 EMA {'rising' if direction=='LONG' else 'falling'} ({ema20:.2f}), "
            f"RVOL {rvol_val:.1f}x, pullback held {'9 EMA' if direction=='LONG' else '9 EMA'} "
            f"({ema9:.2f}). Break of trigger bar {'high' if direction=='LONG' else 'low'}."
        )

        return TradeSetup(
            symbol=symbol,
            strategy=self.name,
            direction=direction,
            entry_price=round(entry, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            partial_target=round(partial, 4),
            risk_per_share=round(risk_per_share, 4),
            reward_per_share=round(reward_per_share, 4),
            reward_risk_ratio=round(rr, 2),
            shares=round(shares, 4),
            risk_czk=round(risk_czk, 2),
            cost_czk=round(cost_czk, 2),
            vwap=round(vwap, 4),
            ema9=round(ema9, 4),
            ema20=round(ema20, 4),
            atr=round(atr_val, 4),
            rvol_val=round(rvol_val, 2),
            rsi2=round(rsi2, 1),
            reasoning=reason,
            timestamp=datetime.now(),
        )

    # ── POSITION MANAGEMENT ───────────────────────────────────────────────────
    def manage_position(
        self, trade, current_price: float, df_1m: pd.DataFrame
    ) -> tuple[str, float]:
        """
        PDF p.3: "scale out 40-60% at 1R, move stop to breakeven only after
        the market proves itself, then trail the remainder beneath the 9 EMA,
        the prior two-bar low, or a multiple of ATR"
        Secondary exit: "9 EMA break, VWAP loss, or time stop after 3-5 bars"
        """
        df = add_all(df_1m, self.cfg)
        row = df.iloc[-1]
        ema9 = row["ema9"]
        vwap = row["vwap"]

        direction = trade.direction
        entry = trade.entry_price
        stop = trade.current_stop
        target = trade.target_price
        partial = trade.partial_target
        risk = abs(entry - stop)

        # ── Hard stop ─────────────────────────────────────────────────────────
        if direction == "LONG" and current_price <= stop:
            return "STOP_HIT", stop
        if direction == "SHORT" and current_price >= stop:
            return "STOP_HIT", stop

        # ── Primary target ────────────────────────────────────────────────────
        if direction == "LONG" and current_price >= target:
            return "TARGET_HIT", target
        if direction == "SHORT" and current_price <= target:
            return "TARGET_HIT", target

        # ── Move stop to breakeven after 1R ──────────────────────────────────
        if not trade.stop_at_be:
            if direction == "LONG" and current_price >= entry + risk:
                return "MOVE_BE", entry
            if direction == "SHORT" and current_price <= entry - risk:
                return "MOVE_BE", entry

        # ── Trail under 9 EMA (PDF: "trail remainder beneath the 9 EMA") ─────
        if trade.stop_at_be:
            if direction == "LONG":
                new_trail = ema9 - 0.01
                if new_trail > stop:
                    return "TRAIL", new_trail
            else:
                new_trail = ema9 + 0.01
                if new_trail < stop:
                    return "TRAIL", new_trail

        # ── Secondary exits ────────────────────────────────────────────────────
        if direction == "LONG":
            if current_price < vwap:
                return "EXIT_SIGNAL", current_price  # VWAP lost
            if current_price < ema9:
                return "EXIT_SIGNAL", current_price  # 9 EMA broken
        else:
            if current_price > vwap:
                return "EXIT_SIGNAL", current_price
            if current_price > ema9:
                return "EXIT_SIGNAL", current_price

        return "HOLD", stop
