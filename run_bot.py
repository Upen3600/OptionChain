"""
╔══════════════════════════════════════════════════════════════════╗
║   OC CONFLUENCE SCANNER — Railway Launcher                       ║
║   Auto-login → Dashboard → Tick-by-Tick Scanner                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import subprocess
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  PLAYWRIGHT INSTALL  (Railway cold start)
# ─────────────────────────────────────────────
def ensure_playwright():
    browser_path = os.path.expanduser("~/.cache/ms-playwright")
    ready = False
    if os.path.exists(browser_path):
        for _, _, files in os.walk(browser_path):
            if any("chrome" in f.lower() for f in files):
                ready = True
                break
    if not ready:
        log.info("Installing Playwright Chromium...")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=True, timeout=300)
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"],
                           check=True, timeout=120)
        except Exception:
            pass
        log.info("Playwright ready.")
    else:
        log.info("Playwright already installed.")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def start():
    print("\n" + "═" * 60)
    print("  ⚡ OC CONFLUENCE SCANNER — Railway Deployment")
    print("  📊 Nifty | BankNifty | FinNifty | Sensex")
    print("  🌐 Web Dashboard — Tick-by-Tick Live")
    print("═" * 60 + "\n")

    ensure_playwright()

    from kite_auto_login import get_access_token
    from dashboard      import run_dashboard, start_ticker, set_active_trade, push_scan_update
    from scanner        import OCSScanner

    # ── Start web dashboard in background ──
    threading.Thread(target=run_dashboard, daemon=True).start()
    log.info("Dashboard thread started.")

    # ── Login ──
    log.info("Logging in to Kite...")
    try:
        token = get_access_token()
        log.info("Token obtained.")
    except Exception as e:
        log.critical(f"Login failed: {e}")
        import requests as _r
        _r.post(
            f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_TOKEN','8620220458:AAG-oxvhWhPio7iX9pWCk-0AFovl5KrUXxc')}/sendMessage",
            json={"chat_id": os.environ.get("TELEGRAM_CHAT_ID", "5112248039"),
                  "text": f"❌ <b>Scanner Failed to Start</b>\n{str(e)[:200]}",
                  "parse_mode": "HTML"}, timeout=10
        )
        sys.exit(1)

    # ── Init scanner ──
    scanner = OCSScanner(
        access_token=token,
        on_trade_open=lambda sym, tr: set_active_trade(sym, tr),
        on_trade_close=lambda sym: set_active_trade(sym, None),
        on_scan_update=lambda sym, data: push_scan_update(sym, data),
    )

    # ── Start KiteTicker (tick-by-tick) ──
    start_ticker(token, scanner)
    log.info("KiteTicker started.")

    # ── Daily token refresh at 8:45 AM IST ──
    def token_refresh_loop():
        while True:
            now = datetime.now(IST)
            # Sleep until next 8:45 IST
            target_h, target_m = 8, 45
            secs_now = now.hour * 3600 + now.minute * 60 + now.second
            secs_tgt = target_h * 3600 + target_m * 60
            sleep_s  = secs_tgt - secs_now
            if sleep_s <= 0:
                sleep_s += 86400
            log.info(f"Token refresh in {sleep_s//3600}h {(sleep_s%3600)//60}m")
            time.sleep(sleep_s)
            log.info("Refreshing token (8:45 AM IST)...")
            try:
                new_token = get_access_token(force_refresh=True)
                scanner.set_token(new_token)
                start_ticker(new_token, scanner)
                log.info("Token refreshed.")
            except Exception as e:
                log.error(f"Token refresh failed: {e}")

    threading.Thread(target=token_refresh_loop, daemon=True).start()

    # ── Keep alive ──
    log.info("All systems running.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    start()
