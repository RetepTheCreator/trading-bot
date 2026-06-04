"""
Market data via yfinance (default) and optionally Alpaca.
USD/CZK rate fetched at startup.
"""
from __future__ import annotations
import time
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf

from config import TradingConfig

ET = pytz.timezone("America/New_York")


def _rename_yf(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise yfinance column names to lowercase."""
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df.rename(columns={"adj close": "close"})
    return df[["open", "high", "low", "close", "volume"]].dropna()


class DataFeed:
    def __init__(self, config: TradingConfig):
        self.config = config
        self._cache: dict[str, tuple[pd.DataFrame, float]] = {}  # symbol → (df, ts)
        self._cache_ttl = 55  # seconds

    # ── FX RATE ───────────────────────────────────────────────────────────────
    def fetch_czk_rate(self) -> float:
        """Fetch current USD/CZK rate."""
        try:
            df = yf.download("USDCZK=X", period="1d", interval="1m", progress=False)
            df = _rename_yf(df)
            if not df.empty:
                rate = float(df["close"].iloc[-1])
                self.config.czk_usd_rate = rate
                return rate
        except Exception:
            pass
        return self.config.czk_usd_rate  # fallback to configured default

    # ── BARS ──────────────────────────────────────────────────────────────────
    def get_bars(self, symbol: str, interval: str = "1m") -> Optional[pd.DataFrame]:
        """
        Return today's intraday bars.  Uses a 55-second cache per symbol
        to avoid hitting yfinance rate limits.
        """
        cache_key = f"{symbol}_{interval}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        try:
            df = yf.download(
                symbol, period="1d", interval=interval,
                progress=False, auto_adjust=True
            )
            if df is None or df.empty:
                return None
            df = _rename_yf(df)

            # Ensure timezone-aware DatetimeIndex in ET
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC").tz_convert(ET)
            else:
                df.index = df.index.tz_convert(ET)

            self._cache[cache_key] = (df, time.time())
            return df
        except Exception as e:
            print(f"[DataFeed] Error fetching {symbol}: {e}")
            return None

    def get_bars_5m(self, symbol: str) -> Optional[pd.DataFrame]:
        return self.get_bars(symbol, interval="5m")

    # ── LATEST PRICE ──────────────────────────────────────────────────────────
    def get_price(self, symbol: str) -> Optional[float]:
        df = self.get_bars(symbol, "1m")
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    # ── PRE-MARKET GAP ────────────────────────────────────────────────────────
    def premarket_gap_pct(self, symbol: str) -> float:
        """
        Percentage gap between yesterday's close and today's open.
        Used by mean reversion to reject catalyst-driven moves.
        """
        try:
            df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
            df = _rename_yf(df)
            if len(df) < 2:
                return 0.0
            prev_close = float(df["close"].iloc[-2])
            today_open = float(df["open"].iloc[-1])
            return abs(today_open - prev_close) / prev_close
        except Exception:
            return 0.0

    # ── INVALIDATE CACHE ─────────────────────────────────────────────────────
    def invalidate(self, symbol: str):
        for key in list(self._cache.keys()):
            if key.startswith(symbol):
                del self._cache[key]
