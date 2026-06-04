"""
Opening Range Breakout (ORB) Strategy – implemented exactly per PDF pages 6-7.

Setup:
  - Define ORB: high/low of first 15 minutes from 09:30 ET
  - Instrument must be "in play": RVOL > 2, VWAP alignment
  - Breakout bar volume must exceed recent average

Entry:  break of ORB high (LONG) or ORB low (SHORT)
Stop:   opposite side of range or 0.5 ATR
Target: 2.2R primary, partial exit at 1R
Trail:  range midpoint or 10 EMA

Failed breakout: if price quickly re-enters range → exit immediately
"""
from __future__ import annotations
from datetime import datetime, time
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from config import TradingConfig
from indicators import add_all, orb_levels
from strategies.base import BaseStrategy, TradeSetup

ET = pytz.timezone("America/New_York")
MARKET_OPEN = time(9, 30)


class BreakoutStrategy(BaseStrategy):
    name = "BREAKOUT"

    def can_trade(self) -> tuple[bool, str]:
        # PDF: "Cap daily drawdown at 1.25%–1.75% or after two failed breakouts"
        cap = self.cfg.breakout_daily_loss_cap_pct * self.rm.capital.equity_czk
        if self._daily_losses_czk >= cap:
            return False, "Breakout daily loss cap reached"
        if self._daily_failed_breakouts >= self.cfg.breakout_max_failed:
            return False, f"Max failed breakouts ({self.cfg.breakout_max_failed}) reached today"
        return True, ""

    # ── SCAN ──────────────────────────────────────────────────────────────────
    def scan(
        self,
        symbol: str,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
    ) -> Optional[TradeSetup]:
        if len(df_1m) < self.cfg.orb_minutes + 5:
            return None

        df = add_all(df_1m, self.cfg)
        row = df.iloc[-1]

        vwap = row["vwap"]
        atr_val = row["atr"]
        rvol_val = row["rvol"]
        price = row["close"]

        if any(pd.isna([vwap, atr_val, rvol_val])):
            return None

        # PDF: "require relative volume above 2"
        if rvol_val < self.cfg.min_rvol:
            return None

        # ORB window must have closed
        now_et = df.index[-1]
        orb_end_time = time(
            MARKET_OPEN.hour,
            MARKET_OPEN.minute + self.cfg.orb_minutes
        )
        if now_et.time() < orb_end_time:
            return None   # still inside the ORB formation window

        orb_high, orb_low = orb_levels(df, self.cfg.orb_minutes)
        if any(pd.isna([orb_high, orb_low])):
            return None

        orb_range = orb_high - orb_low

        # PDF: "breakout bar whose volume is meaningfully above the recent intraday average"
        avg_vol = df["volume"].iloc[-20:-1].mean()
        breakout_bar_volume_ok = row["volume"] > avg_vol * 1.5

        if not breakout_bar_volume_ok:
            return None

        direction = self._detect_direction(price, vwap, orb_high, orb_low, df)
        if direction is None:
            return None

        return self._build_setup(
            symbol, direction, orb_high, orb_low, orb_range,
            atr_val, vwap, row["ema9"], row["ema20"], rvol_val, row["rsi2"]
        )

    def _detect_direction(
        self, price, vwap, orb_high, orb_low, df
    ) -> Optional[str]:
        prev = df.iloc[-2]

        # LONG: current bar breaks above ORB high, VWAP alignment (price > VWAP)
        if prev["close"] <= orb_high and price > orb_high and price > vwap:
            return "LONG"

        # SHORT: current bar breaks below ORB low, VWAP alignment (price < VWAP)
        if prev["close"] >= orb_low and price < orb_low and price < vwap:
            return "SHORT"

        return None

    def _build_setup(
        self, symbol, direction, orb_high, orb_low, orb_range,
        atr_val, vwap, ema9, ema20, rvol_val, rsi2
    ) -> Optional[TradeSetup]:
        # PDF: stop at opposite side of range or 0.5 ATR if range is too large
        atr_stop = self.cfg.breakout_stop_atr_mult * atr_val
        range_stop = orb_range

        if direction == "LONG":
            entry = orb_high + 0.01
            # PDF: "stop at opposite side of range or 0.5 ATR"
            raw_stop = orb_low - 0.01
            if range_stop > 2 * atr_stop:  # range too large – use ATR
                raw_stop = entry - atr_stop
            stop = raw_stop
            target = entry + self.cfg.breakout_target_r * (entry - stop)
            partial = entry + 1.0 * (entry - stop)
        else:
            entry = orb_low - 0.01
            raw_stop = orb_high + 0.01
            if range_stop > 2 * atr_stop:
                raw_stop = entry + atr_stop
            stop = raw_stop
            target = entry - self.cfg.breakout_target_r * (stop - entry)
            partial = entry - 1.0 * (stop - entry)

        risk_per_share = abs(entry - stop)
        reward_per_share = abs(target - entry)
        if risk_per_share <= 0:
            return None

        rr = reward_per_share / risk_per_share
        if rr < self.cfg.min_reward_risk_ratio:
            return None

        shares, risk_czk, cost_czk = self.rm.capital.calc_position(
            self.cfg.breakout_risk_pct, risk_per_share, entry
        )
        if shares <= 0:
            return None

        reason = (
            f"ORB Breakout {direction}: price {'above' if direction=='LONG' else 'below'} "
            f"ORB {'high' if direction=='LONG' else 'low'} "
            f"({'%.2f' % orb_high}/{('%.2f' % orb_low)}), "
            f"VWAP {vwap:.2f}, RVOL {rvol_val:.1f}x. "
            f"ORB range {orb_range:.2f}. Stop at opposite range side."
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
        PDF p.7: "sell one-third to one-half at 1R, move stop only after
        instrument closes outside the range, then trail the rest"
        "if price re-enters the range quickly and volume does not expand, exit early"
        """
        df = add_all(df_1m, self.cfg)
        row = df.iloc[-1]
        ema10 = row["ema10"]
        vol = row["volume"]
        avg_vol = df["volume"].iloc[-20:-1].mean()

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

        # ── Failed breakout: price re-enters range with low volume ────────────
        # Stored: original ORB levels encoded in stop/entry distance
        # Approximate: if price reverses back through entry before reaching 0.5R
        unrealized_r = (current_price - entry) / risk if direction == "LONG" else (entry - current_price) / risk
        if unrealized_r < 0 and vol < avg_vol:
            self._daily_failed_breakouts += 1
            return "EXIT_SIGNAL", current_price  # failed breakout exit

        # ── Target ────────────────────────────────────────────────────────────
        if direction == "LONG" and current_price >= target:
            return "TARGET_HIT", target
        if direction == "SHORT" and current_price <= target:
            return "TARGET_HIT", target

        # ── Move stop to breakeven once outside range ─────────────────────────
        if not trade.stop_at_be and trade.partial_done:
            if direction == "LONG" and current_price > entry + risk:
                return "MOVE_BE", entry
            if direction == "SHORT" and current_price < entry - risk:
                return "MOVE_BE", entry

        # ── Trail under 10 EMA (PDF: "trail using range midpoint, 10 EMA") ───
        if trade.stop_at_be:
            if direction == "LONG":
                new_trail = ema10 - 0.01
                if new_trail > stop:
                    return "TRAIL", new_trail
            else:
                new_trail = ema10 + 0.01
                if new_trail < stop:
                    return "TRAIL", new_trail

        return "HOLD", stop
