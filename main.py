"""
Day Trading Bot – main entry point.

Normal operation (notify only, no input required):
  • Finds a setup that passes all PDF rules → executes immediately → notifies you
  • Stop hit → auto-closes immediately → notifies you
  • Partial profit at 1R → auto-executes → notifies you
  • Move stop to breakeven, trail stop, EOD flatten → all auto → notify

Requires your decision (blocking, waits for y/n):
  • Bot wants to deviate from a PDF strategy rule
  • Signals are conflicting or ambiguous
  • Unusual condition not covered by the research
  • Slippage is materially worse than expected

Run:  python3 main.py
      python3 main.py --broker alpaca
      python3 main.py --watchlist SPY,AAPL,AMD
"""
from __future__ import annotations
import argparse
import os
import signal
import sys
import time
from datetime import datetime, time as dt_time

import pytz

from capital_manager import CapitalManager
from config import TradingConfig
from data_feed import DataFeed
from notifier import Notifier
from order_manager import OrderManager
from approval_gate import ApprovalGate
from risk_manager import RiskManager
from strategies import BreakoutStrategy, MomentumStrategy, MeanReversionStrategy

ET = pytz.timezone("America/New_York")
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)
EOD_FLATTEN = dt_time(15, 55)


class TradingBot:
    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg
        self.notifier = Notifier(cfg)
        self.capital = CapitalManager(cfg)
        self.data = DataFeed(cfg)
        self.risk = RiskManager(cfg, self.capital)
        self.orders = OrderManager(cfg, self.capital, self.notifier)
        self.gate = ApprovalGate(cfg, self.notifier, self.risk, self.capital)

        # PDF priority order: breakout first, then momentum, then mean reversion
        self.strategies = [
            BreakoutStrategy(cfg, self.risk),
            MomentumStrategy(cfg, self.risk),
            MeanReversionStrategy(cfg, self.risk),
        ]

        self._running = True
        self._last_scan: float = 0.0
        self._last_pos_check: float = 0.0
        self._day_initialized = False
        self._daily_limit_alerted = False

    # ── MARKET HOURS ──────────────────────────────────────────────────────────
    def _now_et(self) -> datetime:
        return datetime.now(ET)

    def _market_open(self) -> bool:
        now = self._now_et()
        if now.weekday() >= 5:
            return False
        return MARKET_OPEN <= now.time() <= MARKET_CLOSE

    def _eod_approaching(self) -> bool:
        return self._now_et().time() >= EOD_FLATTEN

    # ── DAILY INIT ────────────────────────────────────────────────────────────
    def _start_of_day(self):
        if self._day_initialized:
            return
        self.notifier.banner("Market Open – Bot Active")

        rate = self.data.fetch_czk_rate()
        self.notifier.info(f"USD/CZK rate: {rate:.2f}")

        # Fetch pre-market gaps for the mean-reversion news filter
        for sym in self.cfg.watchlist:
            gap = self.data.premarket_gap_pct(sym)
            self.risk.set_gap(sym, gap)
            if gap > 0.02:
                self.notifier.info(
                    f"  {sym}: pre-market gap {gap*100:.1f}% → "
                    f"mean-reversion disabled for this symbol today"
                )

        for s in self.strategies:
            s.reset_daily()
        self.capital.reset_daily()
        self.risk.reset_daily()
        self._daily_limit_alerted = False

        snap = self.capital.snapshot()
        self.notifier.info(
            f"Equity: {snap['equity_czk']} CZK | "
            f"Cash: {snap['cash_czk']} CZK | "
            f"Open: {snap['open_positions']} positions"
        )
        self._day_initialized = True

    def _end_of_day(self):
        if not self._day_initialized:
            return
        self.notifier.banner("Market Closing – Flattening All Positions")
        for trade_id in list(self.capital.open_trades.keys()):
            trade = self.capital.open_trades[trade_id]
            price = self.data.get_price(trade.symbol) or trade.entry_price
            self.orders.close_position(trade_id, price, "EOD_FLATTEN")
        snap = self.capital.snapshot()
        self.notifier.banner(
            f"Day Summary | Equity: {snap['equity_czk']} CZK | "
            f"Day P&L: {snap['daily_realized_pnl_czk']:+.2f} CZK "
            f"({snap['daily_pnl_pct']:+.2f}%)"
        )
        self._day_initialized = False

    # ── SCANNING ──────────────────────────────────────────────────────────────
    def _scan(self):
        if self.risk.global_daily_limit_reached():
            if not self._daily_limit_alerted:
                self.notifier.alert(
                    "DAILY LOSS LIMIT REACHED – no new trades for the rest of today."
                )
                self._daily_limit_alerted = True
            return

        for symbol in self.cfg.watchlist:
            # One position per symbol at a time
            if symbol in {t.symbol for t in self.capital.open_trades.values()}:
                continue

            df_1m = self.data.get_bars(symbol, "1m")
            df_5m = self.data.get_bars_5m(symbol)
            if df_1m is None or df_5m is None or len(df_1m) < 30:
                continue

            setups_found = []
            for strategy in self.strategies:
                can, reason = strategy.can_trade()
                if not can:
                    continue
                try:
                    setup = strategy.scan(symbol, df_1m, df_5m)
                except Exception as e:
                    self.notifier.alert(
                        f"[{strategy.name}] scan error on {symbol}: {e}"
                    )
                    continue
                if setup is not None:
                    setups_found.append((strategy, setup))

            if len(setups_found) == 0:
                continue  # nothing to do, keep monitoring silently

            if len(setups_found) > 1:
                # More than one strategy triggered on the same symbol at the same time.
                # This is ambiguous – ask the user which to take (Rule 6).
                names = [s.name for s, _ in setups_found]
                chosen = self.gate.request_decision(
                    subject=f"Conflicting signals on {symbol}",
                    detail=(
                        f"Strategies triggered simultaneously: {', '.join(names)}. "
                        f"Bot will take the highest-priority signal ({names[0]}) "
                        f"unless you deny."
                    ),
                    default_deny=False,  # default: take priority signal
                )
                if not chosen:
                    self.notifier.info(
                        f"Conflicting signals on {symbol} – skipped by user decision."
                    )
                    continue
                # Take the first (highest-priority) setup
                setups_found = [setups_found[0]]

            strategy, setup = setups_found[0]

            # Validate and execute – no blocking input for clean setups
            ok, reason = self.gate.validate_and_notify(setup)
            if not ok:
                self.notifier.info(f"Setup rejected ({reason}): {symbol} {setup.strategy}")
                continue

            trade_id = self.orders.open_position(setup)
            if not trade_id:
                self.notifier.alert(f"Order failed for {symbol} – see logs.")

    # ── POSITION MONITORING ───────────────────────────────────────────────────
    def _monitor_positions(self):
        for trade_id, trade in list(self.capital.open_trades.items()):
            price = self.data.get_price(trade.symbol)
            if price is None:
                continue
            df_1m = self.data.get_bars(trade.symbol, "1m")
            if df_1m is None:
                continue

            strategy = self._strategy_for(trade.strategy)
            if strategy is None:
                # Unknown strategy name in a saved trade – ask user
                self.gate.request_decision(
                    subject=f"Unrecognised strategy '{trade.strategy}' on {trade.symbol}",
                    detail="Cannot manage this position automatically. Close it?",
                    default_deny=False,
                )
                continue

            try:
                action, value = strategy.manage_position(trade, price, df_1m)
            except Exception as e:
                self.notifier.alert(
                    f"Position management error on {trade.symbol}: {e}. "
                    f"Holding position until next check."
                )
                continue

            self._handle_action(trade_id, trade, action, value, price, strategy)

    def _handle_action(
        self, trade_id, trade, action: str, value: float,
        current_price: float, strategy
    ):
        if action == "HOLD":
            return

        if action == "STOP_HIT":
            # Auto-execute – notify only (Rule 3: capital must be protected)
            self.notifier.alert(
                f"STOP HIT: {trade.symbol} {trade.direction} "
                f"@ ${current_price:.2f} (stop: ${trade.current_stop:.2f})"
            )
            pnl = self.orders.close_position(trade_id, current_price, "STOP_HIT")
            strategy.record_loss(pnl)

        elif action == "TARGET_HIT":
            if not trade.partial_done:
                # Partial profit at 1R – pre-defined, auto-execute + notify
                pct = (
                    self.cfg.breakout_partial_pct
                    if trade.strategy == "BREAKOUT"
                    else self.cfg.momentum_partial_pct
                    if trade.strategy == "MOMENTUM"
                    else 0.60
                )
                self.orders.partial_close(trade_id, current_price, pct)
                self.capital.open_trades[trade_id].partial_done = True
            else:
                # Final target reached – auto-close + notify
                self.orders.close_position(trade_id, current_price, "TARGET_HIT")

        elif action == "MOVE_BE":
            # Auto-execute – notify only
            self.capital.update_stop(trade_id, value)
            self.capital.open_trades[trade_id].stop_at_be = True
            self.notifier.info(
                f"STOP → BREAKEVEN: {trade.symbol} stop moved to ${value:.2f}"
            )

        elif action == "TRAIL":
            # Auto-execute – notify only when the stop actually moves
            if value != trade.current_stop:
                self.capital.update_stop(trade_id, value)
                self.notifier.info(
                    f"TRAIL: {trade.symbol} stop moved ${trade.current_stop:.2f} → ${value:.2f}"
                )

        elif action == "EXIT_SIGNAL":
            # Strategy says exit (VWAP lost, EMA broken, time stop).
            # This IS part of the strategy – auto-execute + notify.
            self.notifier.trade(
                f"EXIT SIGNAL: {trade.symbol} {trade.direction} "
                f"@ ${current_price:.2f} (strategy exit rule triggered)"
            )
            pnl = self.orders.close_position(trade_id, current_price, "EXIT_SIGNAL")
            if pnl < 0:
                strategy.record_loss(pnl)

        elif action == "TIME_STOP":
            # Also part of the strategy – auto-execute + notify
            self.notifier.trade(
                f"TIME STOP: {trade.symbol} – no progress after allotted bars. "
                f"Closing @ ${current_price:.2f}"
            )
            pnl = self.orders.close_position(trade_id, current_price, "TIME_STOP")
            if pnl < 0:
                strategy.record_loss(pnl)

        else:
            # Unknown action – this should never happen with known strategies,
            # but if it does, ask the user (Rule 6).
            self.gate.request_decision(
                subject=f"Unknown position action '{action}' on {trade.symbol}",
                detail=(
                    f"Strategy {trade.strategy} returned an unrecognised action. "
                    f"Current price ${current_price:.2f}. Close the position?"
                ),
                default_deny=False,
            )

    def _strategy_for(self, name: str):
        for s in self.strategies:
            if s.name == name:
                return s
        return None

    # ── MAIN LOOP ─────────────────────────────────────────────────────────────
    def run(self, max_runtime_minutes: int = 0):
        """
        max_runtime_minutes: when >0, bot flattens all positions and exits
        cleanly after this many minutes. Used by GitHub Actions to exit
        before the 6-hour job limit kills the process mid-trade.
        """
        self.notifier.banner(
            f"Day Trading Bot | {self.cfg.starting_capital_czk:.0f} CZK | "
            f"{self.cfg.broker.upper()} | "
            f"Watchlist: {', '.join(self.cfg.watchlist)}"
        )
        self.notifier.info(
            "Strategies: BREAKOUT → MOMENTUM → MEAN_REVERSION (PDF priority order)"
        )
        self.notifier.info(
            "Bot trades autonomously within the PDF rules. "
            "You are notified of every action. "
            "Your input is only needed for deviations or conflicts."
        )
        if max_runtime_minutes:
            self.notifier.info(
                f"Max runtime: {max_runtime_minutes} min "
                f"(will flatten positions and exit cleanly before then)"
            )
        self.notifier.divider()

        start_time = time.time()
        # Flatten positions 5 minutes before the hard runtime limit
        flatten_at = (max_runtime_minutes - 5) * 60 if max_runtime_minutes else None

        def _shutdown(sig, frame):
            self.notifier.banner("Shutdown – closing all open positions…")
            for trade_id, trade in list(self.capital.open_trades.items()):
                price = self.data.get_price(trade.symbol) or trade.entry_price
                self.orders.close_position(trade_id, price, "MANUAL_SHUTDOWN")
            snap = self.capital.snapshot()
            self.notifier.info(f"Final equity: {snap['equity_czk']} CZK")
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        while self._running:
            now = time.time()
            elapsed = now - start_time

            # ── Graceful runtime-limit exit ───────────────────────────────────
            if flatten_at and elapsed >= flatten_at and not getattr(self, '_runtime_flattened', False):
                self.notifier.banner(
                    f"Approaching max runtime ({max_runtime_minutes} min). "
                    f"Flattening all positions now."
                )
                self._end_of_day()
                self._runtime_flattened = True

            if max_runtime_minutes and elapsed >= max_runtime_minutes * 60:
                self.notifier.banner("Max runtime reached – exiting. Bot will restart tomorrow.")
                sys.exit(0)

            market_open = self._market_open()

            if market_open and not self._day_initialized:
                self._start_of_day()

            if not market_open and self._day_initialized:
                self._end_of_day()

            if market_open:
                if self._eod_approaching() and self.capital.open_trades:
                    self._end_of_day()

                elif (now - self._last_scan) >= self.cfg.scan_interval_sec:
                    self._scan()
                    self._last_scan = now

                if (now - self._last_pos_check) >= self.cfg.position_check_interval_sec:
                    self._monitor_positions()
                    self._last_pos_check = now

            else:
                now_et = self._now_et()
                next_check = 60
                if now_et.weekday() < 5:
                    self.notifier.info(
                        f"Market closed ({now_et.strftime('%H:%M ET')}). "
                        f"Waiting for 09:30 ET open."
                    )
                else:
                    self.notifier.info(
                        f"Weekend ({now_et.strftime('%A')}). Bot resumes Monday 09:30 ET."
                    )
                    next_check = 3600
                time.sleep(next_check)
                continue

            time.sleep(1)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _run_demo(cfg: TradingConfig):
    """
    Demo mode: fetch the most recent available market data and run one full
    scan cycle so you can see the bot's output without waiting for market open.
    Uses yfinance 5-day history so yesterday's intraday bars are available.
    No real orders are placed.
    """
    import yfinance as yf
    import pandas as pd
    import pytz

    cfg.broker = "paper"  # always paper in demo
    ET = pytz.timezone("America/New_York")

    notifier = Notifier(cfg)
    capital = CapitalManager(cfg)
    data = DataFeed(cfg)
    risk = RiskManager(cfg, capital)
    orders = OrderManager(cfg, capital, notifier)
    gate = ApprovalGate(cfg, notifier, risk, capital)

    strategies = [
        BreakoutStrategy(cfg, risk),
        MomentumStrategy(cfg, risk),
        MeanReversionStrategy(cfg, risk),
    ]

    notifier.banner(f"DEMO SCAN | {cfg.starting_capital_czk:.0f} CZK | Paper | {', '.join(cfg.watchlist)}")
    notifier.info("Fetching most recent intraday data (last trading day)…")

    rate = data.fetch_czk_rate()
    notifier.info(f"USD/CZK: {rate:.2f}")
    notifier.divider()

    any_setup = False
    for symbol in cfg.watchlist:
        notifier.info(f"Scanning {symbol}…")
        # Fetch last 5 days of 1-minute data; use only the most recent complete session
        try:
            raw = yf.download(symbol, period="5d", interval="1m", progress=False, auto_adjust=True)
            if raw is None or raw.empty:
                notifier.info(f"  {symbol}: no data available")
                continue
            raw.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in raw.columns]
            raw = raw.rename(columns={"adj close": "close"})
            raw = raw[["open", "high", "low", "close", "volume"]].dropna()
            if raw.index.tz is None:
                raw.index = raw.index.tz_localize("UTC").tz_convert(ET)
            else:
                raw.index = raw.index.tz_convert(ET)

            # Pick the last complete trading session (yesterday or last Friday)
            dates = sorted(raw.index.date, reverse=True)
            target_date = dates[1] if len(dates) > 1 else dates[0]
            df_1m = raw[raw.index.date == target_date].copy()
            df_5m_raw = yf.download(symbol, period="5d", interval="5m", progress=False, auto_adjust=True)
            if df_5m_raw is not None and not df_5m_raw.empty:
                df_5m_raw.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df_5m_raw.columns]
                df_5m_raw = df_5m_raw.rename(columns={"adj close": "close"})
                df_5m_raw = df_5m_raw[["open", "high", "low", "close", "volume"]].dropna()
                if df_5m_raw.index.tz is None:
                    df_5m_raw.index = df_5m_raw.index.tz_localize("UTC").tz_convert(ET)
                else:
                    df_5m_raw.index = df_5m_raw.index.tz_convert(ET)
                df_5m = df_5m_raw[df_5m_raw.index.date == target_date].copy()
            else:
                df_5m = df_1m.resample("5min").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                ).dropna()

            if len(df_1m) < 30:
                notifier.info(f"  {symbol}: only {len(df_1m)} bars – need ≥30, skipping")
                continue

            # Set gap for mean-reversion filter
            gap = data.premarket_gap_pct(symbol)
            risk.set_gap(symbol, gap)

            notifier.info(f"  {symbol}: {len(df_1m)} bars from {target_date}  gap={gap*100:.1f}%")

            for strategy in strategies:
                try:
                    setup = strategy.scan(symbol, df_1m, df_5m)
                except Exception as e:
                    notifier.info(f"  [{strategy.name}] error: {e}")
                    continue

                if setup is None:
                    notifier.info(f"  [{strategy.name}] no setup")
                    continue

                any_setup = True
                ok, reason = gate.validate_and_notify(setup)
                if not ok:
                    notifier.info(f"  Setup rejected: {reason}")
                else:
                    notifier.info(
                        f"  ✓ Setup valid – in live mode this would execute immediately."
                    )
                break  # one strategy per symbol

        except Exception as e:
            notifier.alert(f"  {symbol}: fetch error – {e}")

    notifier.divider()
    if not any_setup:
        notifier.info(
            "No setups found on last session's data. "
            "This is normal – the bot only trades when ALL conditions are met."
        )
    snap = capital.snapshot()
    notifier.info(f"Capital: {snap['equity_czk']} CZK (unchanged – demo mode, no orders placed)")
    notifier.banner("Demo complete. Run  python3 main.py  to start live monitoring.")


def main():
    parser = argparse.ArgumentParser(description="Day Trading Bot")
    parser.add_argument("--broker", choices=["paper", "alpaca"], default="paper")
    parser.add_argument("--watchlist", default=None, help="SPY,AAPL,AMD")
    parser.add_argument("--capital", type=float, default=None, help="Starting CZK")
    parser.add_argument(
        "--demo", action="store_true",
        help="Run one scan cycle on last session's data (no orders, no market hours check)"
    )
    args = parser.parse_args()

    cfg = TradingConfig()
    if args.broker:
        cfg.broker = args.broker
    if args.watchlist:
        cfg.watchlist = [s.strip().upper() for s in args.watchlist.split(",")]
    if args.capital:
        cfg.starting_capital_czk = args.capital

    if cfg.starting_capital_czk < 500:
        print(
            "\n⚠  Capital below 500 CZK (~$22). Positions will be at the broker minimum ($1).\n"
            "   Use Alpaca paper trading first to accumulate paper gains before live.\n"
        )

    if args.demo:
        _run_demo(cfg)
    else:
        max_runtime = int(os.getenv("MAX_RUNTIME_MINUTES", 0))
        TradingBot(cfg).run(max_runtime_minutes=max_runtime)


if __name__ == "__main__":
    main()
