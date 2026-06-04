"""
All user-facing output and logging.
Terminal output always. Telegram notification when token is configured.
macOS desktop alerts as a bonus when running locally.
"""
from __future__ import annotations
import os
import subprocess
from datetime import datetime

import requests
from colorama import Fore, Style, init

from config import TradingConfig

init(autoreset=True)


class Notifier:
    def __init__(self, config: TradingConfig):
        self.cfg = config
        os.makedirs(config.log_dir, exist_ok=True)
        log_name = datetime.now().strftime("%Y-%m-%d") + "_bot.log"
        self._log_path = os.path.join(config.log_dir, log_name)
        self._tg_ok = bool(config.telegram_token and config.telegram_chat_id)
        if self._tg_ok:
            print(f"[Notifier] Telegram notifications enabled → chat {config.telegram_chat_id}")

    # ── INTERNAL ──────────────────────────────────────────────────────────────
    def _write(self, level: str, msg: str) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        try:
            with open(self._log_path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
        return line

    def _telegram(self, text: str):
        if not self._tg_ok:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json={"chat_id": self.cfg.telegram_chat_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def _macos(self, title: str, body: str):
        try:
            body_s = body.replace('"', "'")[:160]
            title_s = title.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body_s}" with title "{title_s}"'],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    # ── PUBLIC ────────────────────────────────────────────────────────────────
    def info(self, msg: str):
        line = self._write("INFO", msg)
        print(Fore.WHITE + line)

    def alert(self, msg: str):
        line = self._write("ALERT", msg)
        print(Fore.YELLOW + Style.BRIGHT + line)
        self._telegram(f"⚠️ {msg}")
        self._macos("Bot Alert", msg)

    def trade(self, msg: str):
        line = self._write("TRADE", msg)
        print(Fore.CYAN + Style.BRIGHT + line)
        self._telegram(f"📊 {msg}")
        self._macos("Trade", msg)

    def setup_found(self, msg: str):
        line = self._write("SETUP", msg)
        print(Fore.GREEN + Style.BRIGHT + line)
        self._telegram(f"✅ EXECUTED: {msg}")
        self._macos("Trade Executed", msg)

    def denied(self, msg: str):
        line = self._write("DENIED", msg)
        print(Fore.RED + line)

    def decision_needed(self, msg: str):
        """Sent when human input is required (deviation / uncertainty)."""
        line = self._write("DECISION", msg)
        print(Fore.YELLOW + Style.BRIGHT + line)
        self._telegram(f"🔔 DECISION NEEDED:\n{msg}")
        self._macos("Decision Required", msg)

    def divider(self):
        print(Fore.WHITE + Style.DIM + "─" * 70)

    def banner(self, msg: str):
        print(Fore.MAGENTA + Style.BRIGHT + f"\n{'═'*70}")
        print(Fore.MAGENTA + Style.BRIGHT + f"  {msg}")
        print(Fore.MAGENTA + Style.BRIGHT + f"{'═'*70}\n")
        self._telegram(f"🤖 {msg}")
