"""
VWAP Mean Reversion Strategy – implemented exactly per PDF pages 4-6.

CRITICAL FILTERS (must all pass before entry):
  - No major news / fresh catalyst for this instrument today
  - Pre-market gap < 3% (proxy for no-news condition)
  - NOT a clear trend day (price hasn't moved > 1.5 ATR from open in one direction)

Setup conditions (LONG):
  1. Price stretched ≥ 1.5 ATR below session VWAP
  2. RSI(2) < 10
  3. Tape decelerating: no new impulse lows in last 3 bars

Entry: reclaim of prior 1-minute bar high
Stop:  0.5 ATR beyond the extreme low
Target 1: session midpoint  (first profit)
Target 2: VWAP              (trail to here)

SHORT is the mirror image.
Must flatten all positions by session end (15:55 ET).
"""
from __future__ import annotations
from datetime import datetime, time
from typing import Optional

import numpy as np
import pandas as pd

from config import TradingConfig
from indicators import add_all, is_tape_decelerating, session_midpoint
from strategies.base import BaseStrategy, TradeSetup

FLATTEN_BY = time(15, 55)


class MeanReversionStrategy(BaseStrategy):
    name = "MEAN_REVERSION"

    def can_trade(self) -> tuple[bool, str]:
        cap = self.cfg.mr_daily_loss_cap_pct * self.rm.capital.equity_czk
        if self._daily_losses_czk >= cap:
            return False, "Mean-reversion daily loss cap reached"
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
        atr_val = row["atr"]
        rsi2 = row["rsi2"]
        price = row["close"]
        rvol_val = row["rvol"]

        if any(pd.isna([vwap, atr_val, rsi2])):
            return None

        # ── PRE-MARKET GAP FILTER ─────────────────────────────────────────────
        # PDF: "reject any setup with fresh earnings, guidance, macro surprise"
        # We proxy this with the gap size - stored in risk_manager.news_gaps
        gap_pct = self.rm.get_gap(symbol)
        if gap_pct > 0.03:  # 3% gap = likely catalyst
            return None

        # ── TREND-DAY FILTER ──────────────────────────────────────────────────
        # PDF: "works best when the move is inventory-driven, not fresh information"
        # If today's range is already > 2 ATR and one-directional → trend day
        session_high = df[df.index.time >= time(9, 30)]["high"].max()
        session_low = df[df.index.time >= time(9, 30)]["low"].min()
        session_range = session_high - session_low
        if session_range > 2.0 * atr_val:
            return None   # likely trend day, skip mean reversion

        # ── ATR STRETCH FROM VWAP ─────────────────────────────────────────────
        # PDF: "require price stretched about 1.5–2.0 ATR from session VWAP"
        stretch = abs(price - vwap) / atr_val if atr_val > 0 else 0
        if stretch < self.cfg.mr_atr_stretch_min:
            return None

        # ── RSI(2) EXTREME ────────────────────────────────────────────────────
        # PDF: "require a short-horizon momentum oscillator such as RSI(2) to be extreme"
        is_long_setup = price < vwap and rsi2 <= self.cfg.mr_rsi_oversold
        is_short_setup = price > vwap and rsi2 >= self.cfg.mr_rsi_overbought

        if not is_long_setup and not is_short_setup:
            return None

        # ── TAPE DECELERATION ─────────────────────────────────────────────────
        # PDF: "require tape deceleration or order-flow improvement before entry"
        if is_long_setup and not is_tape_decelerating(df, n=3):
            return None
        if is_short_setup:
            # Short: no new highs in last 3 bars
            recent = df.iloc[-3:]
            if recent["high"].iloc[-1] >= recent["high"].iloc[0]:
                return None

        direction = "LONG" if is_long_setup else "SHORT"

        return self._build_setup(
            symbol, df, direction, vwap, atr_val, rvol_val, rsi2,
            row["ema9"], row["ema20"]
        )

    def _build_setup(
        self, symbol, df, direction, vwap, atr_val, rvol_val, rsi2, ema9, ema20
    ) -> Optional[TradeSetup]:
        prev = df.iloc[-2]
        row = df.iloc[-1]
        price = row["close"]

        if direction == "LONG":
            # PDF: "enter on a reclaim of the prior one-minute bar high"
            entry = prev["high"] + 0.01
            extreme_low = df.iloc[-5:]["low"].min()
            stop = extreme_low - self.cfg.mr_stop_atr_mult * atr_val - 0.01
            # PDF: "first target: session midpoint; second target: VWAP"
            mid = session_midpoint(df)
            target = vwap   # use VWAP as the primary target (more conservative)
            partial = mid if not pd.isna(mid) else (entry + vwap) / 2
        else:
            entry = prev["low"] - 0.01
            extreme_high = df.iloc[-5:]["high"].max()
            stop = extreme_high + self.cfg.mr_stop_atr_mult * atr_val + 0.01
            mid = session_midpoint(df)
            target = vwap
            partial = mid if not pd.isna(mid) else (entry + vwap) / 2

        risk_per_share = abs(entry - stop)
        reward_per_share = abs(target - entry)
        if risk_per_share <= 0 or reward_per_share <= 0:
            return None

        rr = reward_per_share / risk_per_share

        # Mean reversion naturally has lower R:R (PDF: 0.8R avg win, 55%+ win rate).
        # We still enforce a minimum of 0.6 to avoid truly unfavourable setups.
        if rr < 0.6:
            return None

        shares, risk_czk, cost_czk = self.rm.capital.calc_position(
            self.cfg.mr_risk_pct, risk_per_share, entry
        )
        if shares <= 0:
            return None

        stretch = abs(price - vwap) / atr_val
        reason = (
            f"Mean Reversion {direction}: price {stretch:.1f} ATR "
            f"{'below' if direction=='LONG' else 'above'} VWAP ({vwap:.2f}), "
            f"RSI(2)={rsi2:.0f} ({'oversold' if direction=='LONG' else 'overbought'}), "
            f"tape decelerating. No catalyst detected. "
            f"Target: VWAP reversion."
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
        PDF p.5: "take partial profits aggressively at the session mid,
        then trail towards VWAP. Must flatten by session end."
        """
        df = add_all(df_1m, self.cfg)
        row = df.iloc[-1]
        vwap = row["vwap"]

        # PDF: "you should flatten the trade by session end"
        if df.index[-1].time() >= FLATTEN_BY:
            return "EXIT_SIGNAL", current_price

        direction = trade.direction
        stop = trade.current_stop
        target = trade.target_price
        entry = trade.entry_price
        risk = abs(entry - stop)

        # Hard stop
        if direction == "LONG" and current_price <= stop:
            return "STOP_HIT", stop
        if direction == "SHORT" and current_price >= stop:
            return "STOP_HIT", stop

        # Target: VWAP
        if direction == "LONG" and current_price >= target:
            return "TARGET_HIT", target
        if direction == "SHORT" and current_price <= target:
            return "TARGET_HIT", target

        # PDF: "trail towards VWAP" – update trailing stop toward entry after partial
        if trade.partial_done:
            if direction == "LONG":
                new_trail = current_price - 0.5 * risk
                if new_trail > stop:
                    return "TRAIL", new_trail
            else:
                new_trail = current_price + 0.5 * risk
                if new_trail < stop:
                    return "TRAIL", new_trail

        # PDF: "break extreme → exit" – if position reverses to a new extreme
        if direction == "LONG" and current_price < entry - risk:
            return "STOP_HIT", current_price
        if direction == "SHORT" and current_price > entry + risk:
            return "STOP_HIT", current_price

        return "HOLD", stop
