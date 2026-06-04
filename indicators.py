"""
Technical indicators used by all three strategies.
All parameter values match the PDF's exact specifications.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from datetime import time


MARKET_OPEN = time(9, 30)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Cumulative VWAP from 09:30 ET, reset each session.
    PDF: "value often approximated by VWAP, the session mid"
    """
    mask = df.index.time >= MARKET_OPEN
    out = pd.Series(np.nan, index=df.index, dtype=float)
    if mask.sum() == 0:
        return out
    s = df[mask]
    tp = (s["high"] + s["low"] + s["close"]) / 3
    cum_tpv = (tp * s["volume"]).cumsum()
    cum_vol = s["volume"].cumsum()
    out[mask] = (cum_tpv / cum_vol.replace(0, np.nan)).values
    return out


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range – PDF: 'use ATR(14) for stop calibration'"""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 2) -> pd.Series:
    """RSI(2) – PDF: 'require a short-horizon momentum oscillator such as RSI(2) to be extreme'"""
    d = series.diff()
    gain = d.where(d > 0, 0.0).ewm(span=period, adjust=False).mean()
    loss = (-d.where(d < 0, 0.0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def rvol(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """
    Relative volume vs. rolling average of prior bars.
    PDF: "require relative volume above 2.0"
    """
    avg = df["volume"].rolling(lookback, min_periods=3).mean().shift(1)
    return df["volume"] / avg.replace(0, np.nan)


def session_midpoint(df: pd.DataFrame) -> float:
    """High + Low of the full session divided by 2."""
    mask = df.index.time >= MARKET_OPEN
    if mask.sum() == 0:
        return float("nan")
    s = df[mask]
    return (s["high"].max() + s["low"].min()) / 2


def orb_levels(df_1m: pd.DataFrame, orb_minutes: int) -> tuple[float, float]:
    """
    Opening Range Breakout high/low.
    PDF: "Define the high and low of the first 5, 15, or 30 minutes"
    """
    mask = df_1m.index.time >= MARKET_OPEN
    session = df_1m[mask]
    orb = session.iloc[:orb_minutes]
    if len(orb) == 0:
        return float("nan"), float("nan")
    return orb["high"].max(), orb["low"].min()


def is_tape_decelerating(df_1m: pd.DataFrame, n: int = 3) -> bool:
    """
    Mean reversion filter: no new impulse lows in last n bars.
    PDF: "require tape deceleration or order-flow improvement before entry"
    """
    recent = df_1m.iloc[-n:]
    return recent["low"].iloc[-1] >= recent["low"].iloc[0]


def add_all(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Compute all indicators and attach to DataFrame."""
    df = df.copy()
    df["vwap"] = session_vwap(df)
    df["ema9"] = ema(df["close"], cfg.ema_fast)
    df["ema20"] = ema(df["close"], cfg.ema_slow)
    df["ema10"] = ema(df["close"], cfg.ema_breakout_trail)
    df["atr"] = atr(df, cfg.atr_period)
    df["rsi2"] = rsi(df["close"], cfg.rsi_period)
    df["rvol"] = rvol(df, cfg.rvol_lookback)
    return df
