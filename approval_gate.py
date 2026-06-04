"""
Approval gate – Rules 2, 6, 8.

AUTONOMOUS (notify only, no input needed):
  • Clean entries that pass all PDF rules
  • Hard stops, partial profits, stop-to-BE, trailing stops, EOD flatten

REQUIRES YOUR DECISION (blocks and waits):
  • Deviation from a PDF strategy rule
  • Conflicting signals on the same symbol
  • Unusual condition not covered by the research

How the decision request reaches you:
  • Terminal (always, useful when running locally)
  • Telegram message if TELEGRAM_TOKEN is set (works from phone when on cloud)

You reply  y / yes  to approve,  n / no  to deny.
Timeout → safe default (skip entry, execute stop).
"""
from __future__ import annotations
import sys
import threading
import time
from typing import Optional

import requests
from colorama import Fore, Style

from capital_manager import CapitalManager
from config import TradingConfig
from notifier import Notifier
from risk_manager import RiskManager
from strategies.base import TradeSetup

_IS_INTERACTIVE = sys.stdin.isatty()   # False on Railway / cloud servers


class ApprovalGate:
    def __init__(
        self,
        config: TradingConfig,
        notifier: Notifier,
        risk_manager: RiskManager,
        capital: CapitalManager,
    ):
        self.cfg = config
        self.notifier = notifier
        self.rm = risk_manager
        self.capital = capital
        self._tg_ok = bool(config.telegram_token and config.telegram_chat_id)
        self._tg_offset: int = 0   # tracks Telegram message position

    # ── FORMAT TRADE CARD ─────────────────────────────────────────────────────
    def _format_card(self, setup: TradeSetup) -> str:
        usd_risk = self.capital.czk_to_usd(setup.risk_czk)
        snap = self.capital.snapshot()
        return (
            f"\n{Fore.CYAN + Style.BRIGHT}{'[ TRADE SIGNAL – EXECUTING ]':^70}"
            f"{Style.RESET_ALL}\n"
            f"  {setup.strategy} {setup.direction} {setup.symbol}\n"
            f"  Entry ${setup.entry_price:.2f} | Stop ${setup.stop_price:.2f} | "
            f"Target ${setup.target_price:.2f}\n"
            f"  Risk {setup.risk_czk:.2f} CZK (${usd_risk:.2f}) | "
            f"R:R {setup.reward_risk_ratio:.2f}:1\n"
            f"  {setup.reasoning[:100]}\n"
            f"  Account: {snap['equity_czk']} CZK | "
            f"Day P&L: {snap['daily_realized_pnl_czk']:+.2f} CZK\n"
        )

    # ── STANDARD TRADE – validate then signal auto-execute ────────────────────
    def validate_and_notify(self, setup: TradeSetup) -> tuple[bool, str]:
        ok, reason = self.rm.validate_setup(setup)
        if ok:
            print(self._format_card(setup))
            self.notifier.setup_found(
                f"{setup.strategy} {setup.direction} {setup.symbol} "
                f"@ ${setup.entry_price:.2f} | R:R {setup.reward_risk_ratio:.2f}"
            )
        return ok, reason

    # ── DECISION REQUIRED – blocks until answer or timeout ────────────────────
    def request_decision(
        self,
        subject: str,
        detail: str,
        default_deny: bool = True,
    ) -> bool:
        """
        Use only when:
          • strategy deviation is needed
          • conflicting signals exist
          • unusual / uncovered condition

        Sends a Telegram message if configured, waits for reply.
        Falls back to terminal input if running locally.
        """
        timeout = self.cfg.approval_timeout_sec
        default_action = "SKIP" if default_deny else "EXECUTE"

        prompt = (
            f"⚠️ DECISION REQUIRED\n\n"
            f"Subject: {subject}\n"
            f"Detail: {detail}\n\n"
            f"Reply YES to approve, NO to deny.\n"
            f"(Timeout {timeout}s → auto-{default_action})"
        )

        self.notifier.decision_needed(f"{subject} — {detail[:80]}")

        if self._tg_ok:
            self._tg_send(prompt)
            answer = self._tg_wait_reply(timeout)
        elif _IS_INTERACTIVE:
            answer = self._terminal_ask(timeout, default_action)
        else:
            # Cloud server, no Telegram configured → use safe default
            self.notifier.alert(
                f"Decision needed but no Telegram configured. "
                f"Using default: {default_action}. ({subject})"
            )
            answer = None

        if answer is None:
            approved = not default_deny
            self.notifier.info(f"Decision timeout/default → {default_action}")
        else:
            approved = answer
            self.notifier.info(f"Decision: {'APPROVED' if approved else 'DENIED'}")

        return approved

    # ── TELEGRAM HELPERS ─────────────────────────────────────────────────────
    def _tg_send(self, text: str):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json={"chat_id": self.cfg.telegram_chat_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def _tg_wait_reply(self, timeout: int) -> Optional[bool]:
        """Poll Telegram for YES/NO reply. Returns True, False, or None (timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{self.cfg.telegram_token}/getUpdates",
                    params={"offset": self._tg_offset, "timeout": 10},
                    timeout=15,
                )
                for upd in resp.json().get("result", []):
                    self._tg_offset = upd["update_id"] + 1
                    txt = upd.get("message", {}).get("text", "").strip().lower()
                    if txt in ("yes", "y"):
                        return True
                    if txt in ("no", "n"):
                        return False
            except Exception:
                time.sleep(3)
        return None

    # ── TERMINAL HELPER ───────────────────────────────────────────────────────
    def _terminal_ask(self, timeout: int, default_action: str) -> Optional[bool]:
        print(
            f"\n{Fore.YELLOW + Style.BRIGHT}"
            f"  [y] Approve   [n] Deny   "
            f"(timeout {timeout}s → auto-{default_action})  > "
            f"{Style.RESET_ALL}",
            end="", flush=True,
        )
        answer: dict[str, Optional[str]] = {"value": None}
        ev = threading.Event()

        def _read():
            try:
                answer["value"] = input().strip().lower()
            except Exception:
                pass
            ev.set()

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        if ev.wait(timeout=timeout) and answer["value"] in ("y", "yes", "n", "no"):
            return answer["value"] in ("y", "yes")
        return None
