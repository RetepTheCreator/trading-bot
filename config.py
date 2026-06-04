"""
All configuration – parameters derived directly from the PDF research document.
Never change strategy parameters without PDF justification.
"""
import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv
load_dotenv()


@dataclass
class TradingConfig:
    # ── CAPITAL ───────────────────────────────────────────────────────────────
    # Rule 3: bot starts with 2000 CZK and CANNOT touch any other funds.
    starting_capital_czk: float = float(os.getenv("STARTING_CAPITAL_CZK", 2000))
    czk_usd_rate: float = 22.5        # refreshed at startup from live FX

    # ── BROKER ────────────────────────────────────────────────────────────────
    # "paper"   → internal simulation, no real orders (default, always safe)
    # "alpaca"  → live/paper orders via Alpaca API
    broker: str = "paper"
    alpaca_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_paper: bool = field(
        default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() == "true"
    )

    # ── MARKET (US Eastern) ───────────────────────────────────────────────────
    # PDF p.2: "regular hours 09:30–16:00 ET"
    market_open_hour: int = 9
    market_open_minute: int = 30
    market_close_hour: int = 16
    market_close_minute: int = 0
    timezone: str = "America/New_York"

    # ── WATCHLIST ─────────────────────────────────────────────────────────────
    watchlist: List[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("WATCHLIST", "SPY,QQQ,AAPL,TSLA,AMD,NVDA").split(",")
    ])

    # ── INDICATORS (PDF p.2-3) ────────────────────────────────────────────────
    ema_fast: int = 9          # trailing / exit reference
    ema_slow: int = 20         # trend-direction filter
    ema_breakout_trail: int = 10  # trailing EMA used by breakout strategy
    atr_period: int = 14
    rsi_period: int = 2        # very short RSI for mean reversion
    rvol_lookback: int = 20    # bars for average-volume calculation
    min_rvol: float = 2.0      # PDF: "require relative volume above 2.0"

    # ── OPENING RANGE (PDF p.6) ───────────────────────────────────────────────
    orb_minutes: int = 15      # first 15 min define the range (5/15/30 valid)

    # ── MOMENTUM PARAMETERS (PDF p.2-3) ──────────────────────────────────────
    # PDF: "risk 0.25%–0.50% of equity per trade"
    momentum_risk_pct: float = 0.0035          # 0.35% (midrange)
    momentum_daily_loss_cap_pct: float = 0.0175  # "1.5%–2.0% of equity"
    momentum_max_losing_trades: int = 3         # "or three full-risk losing trades"
    momentum_target_r: float = 1.8              # PDF: "average win of 1.8R"
    momentum_partial_pct: float = 0.50          # "scale out 40%–60% at 1R"
    momentum_stop_atr_mult: float = 0.75        # "0.5–1.0 ATR"

    # ── BREAKOUT PARAMETERS (PDF p.6-7) ──────────────────────────────────────
    breakout_risk_pct: float = 0.0035
    breakout_daily_loss_cap_pct: float = 0.015   # "1.25%–1.75%"
    breakout_max_failed: int = 2                  # "after two failed breakouts"
    breakout_target_r: float = 2.2               # "average win of 2.2R"
    breakout_partial_pct: float = 0.40
    breakout_stop_atr_mult: float = 0.50          # "0.5 ATR if range too large"

    # ── MEAN REVERSION PARAMETERS (PDF p.4-5) ────────────────────────────────
    mr_risk_pct: float = 0.0028                   # 0.28% (midrange of 0.20–0.35%)
    mr_daily_loss_cap_pct: float = 0.0125
    mr_atr_stretch_min: float = 1.5               # "stretched about 1.5–2.0 ATR"
    mr_rsi_oversold: float = 10.0                 # "RSI(2) falls below roughly 10"
    mr_rsi_overbought: float = 90.0
    mr_target_r: float = 0.8                      # "average win of 0.8R"
    mr_stop_atr_mult: float = 0.50                # "0.4–0.6 ATR beyond extreme"

    # ── GLOBAL RISK (Rule 5) ──────────────────────────────────────────────────
    # "risk/profit ratio shall not exceed 60/40" → profit must be ≥ 1.5× risk
    min_reward_risk_ratio: float = 1.5

    # ── APPROVAL (Rules 2, 6, 8) ──────────────────────────────────────────────
    approval_timeout_sec: int = 120   # seconds before a pending trade is cancelled

    # ── SCANNING ──────────────────────────────────────────────────────────────
    scan_interval_sec: int = 60
    position_check_interval_sec: int = 30

    # ── TELEGRAM ──────────────────────────────────────────────────────────────
    # Get these from the setup guide (BotFather + /getUpdates trick).
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # ── MISC ──────────────────────────────────────────────────────────────────
    log_dir: str = "logs"
    min_trade_usd: float = 1.0   # Alpaca fractional share minimum
