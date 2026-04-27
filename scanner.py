"""
╔══════════════════════════════════════════════════════════════════╗
║   FRIDAY DUAL-BETA — Pattern Engine (Refactored for Railway)     ║
║   BankNifty + Nifty | F5 + Reversal + Mean Reversion            ║
║   SL & Targets: Option Premium Price Based                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import time
import json
import logging
import threading
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from kiteconnect import KiteConnect

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

TRADE_FILE = "/tmp/trades_log.json"

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
API_KEY          = os.environ.get("API_KEY",          "yj3cey9o0ho0gi1b")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "8620220458:AAG-oxvhWhPio7iX9pWCk-0AFovl5KrUXxc")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5112248039")
CANDLE_CHAT_ID   = os.environ.get("CANDLE_CHAT_ID",   "5112248039")  # same or different

BANKNIFTY_TOKEN = 260105
NIFTY_TOKEN     = 256265

# Option SL/Target config (in premium points)
BN_OPT_SL      = 30   # SL: 30 pts below option buy price
BN_OPT_TARGET1 = 50   # Target 1: 50 pts above option buy price
BN_OPT_TRAIL   = 50   # Trail step
BN_OPT_LOT     = 30   # BankNifty lot size / qty per lot

NF_OPT_SL      = 20
NF_OPT_TARGET1 = 30
NF_OPT_TRAIL   = 30
NF_OPT_LOT     = 75   # Nifty lot size

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(msg: str, chat_id: str = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        requests.post(url, json={"chat_id": cid, "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")


def send_candle_alert(msg: str):
    send_telegram(msg, CANDLE_CHAT_ID)


# ─────────────────────────────────────────────
#  MARKET HOURS
# ─────────────────────────────────────────────
def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 30


def market_session() -> str:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return "WEEKEND"
    t = now.hour * 60 + now.minute
    if t < 9 * 60 + 15:  return "PRE-MARKET"
    if t > 15 * 60 + 30: return "CLOSED"
    return "OPEN"


# ─────────────────────────────────────────────
#  ATM + OPTION HELPER
# ─────────────────────────────────────────────
def get_atm_strike(ltp: float, step: int) -> int:
    return int(round(ltp / step) * step)


def get_option_symbol(instruments: list, strike: int, side: str, name: str) -> str:
    """Get nearest expiry option symbol from instruments list."""
    today = date.today()
    best = None
    best_exp = None
    for inst in instruments:
        if inst.get("name", "").upper() != name.upper():
            continue
        if inst.get("instrument_type", "") != side:
            continue
        if int(inst.get("strike", 0)) != strike:
            continue
        exp = inst.get("expiry")
        if not exp:
            continue
        if isinstance(exp, str):
            exp = date.fromisoformat(exp)
        elif hasattr(exp, 'date'):
            exp = exp.date()
        if exp < today:
            continue
        if best_exp is None or exp < best_exp:
            best_exp = exp
            best = inst.get("tradingsymbol", "")
    return best or ""


def get_option_ltp(kite, symbol: str, exchange: str = "NFO") -> float:
    if not symbol:
        return 0.0
    try:
        q = kite.quote([f"{exchange}:{symbol}"])
        return float(q.get(f"{exchange}:{symbol}", {}).get("last_price", 0) or 0)
    except Exception as e:
        log.error(f"Option LTP [{symbol}]: {e}")
        return 0.0


# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["EMA9"]   = ta.ema(df["close"], length=9)
    df["EMA20"]  = ta.ema(df["close"], length=20)
    df["EMA50"]  = ta.ema(df["close"], length=50)
    df["EMA200"] = ta.ema(df["close"], length=200)
    df["RSI"]    = ta.rsi(df["close"], length=14)
    df["ATR3"]   = ta.atr(df["high"], df["low"], df["close"], length=3)
    df["Body"]   = abs(df["close"] - df["open"])
    df["Range"]  = df["high"] - df["low"]
    df["Upper_Wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["Lower_Wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None:
        df["BBL"] = bb.iloc[:, 0]
        df["BBM"] = bb.iloc[:, 1]
        df["BBU"] = bb.iloc[:, 2]
    return df


# ─────────────────────────────────────────────
#  MAIN SCANNER CLASS
# ─────────────────────────────────────────────
class OCSScanner:
    def __init__(self, access_token: str,
                 on_trade_open=None, on_trade_close=None, on_scan_update=None):
        self.kite = KiteConnect(api_key=API_KEY)
        self.kite.set_access_token(access_token)
        self.access_token = access_token

        self.on_trade_open  = on_trade_open  or (lambda s, t: None)
        self.on_trade_close = on_trade_close or (lambda s: None)
        self.on_scan_update = on_scan_update or (lambda s, d: None)

        # Index LTP
        self.index_ltp = {"BANKNIFTY": 0.0, "NIFTY": 0.0}

        # Candle stores
        self._candles   = {"BANKNIFTY": [], "NIFTY": []}
        self._ws_states = {
            "BANKNIFTY": {"current_candle": None, "current_time": None},
            "NIFTY":     {"current_candle": None, "current_time": None},
        }

        # NFO instruments
        self._nfo_instruments = []

        # Active trades dict per symbol
        self.active_trades: dict = {}

        # Pending reversal signals
        self._pending_reversal: dict = {}

        # Mean reversion active
        self._mr_state: dict = {}

        # Scan stats for dashboard
        self.scan_stats = {
            "BANKNIFTY": {"status": "Loading...", "last_scan": "--:--:--"},
            "NIFTY":     {"status": "Loading...", "last_scan": "--:--:--"},
        }

        # Load historical candles + instruments
        threading.Thread(target=self._init_data, daemon=True).start()

    def set_token(self, token: str):
        self.access_token = token
        self.kite.set_access_token(token)

    # ── Returns tokens for KiteTicker subscription ─────────────────
    def get_all_tokens(self) -> list:
        return [BANKNIFTY_TOKEN, NIFTY_TOKEN]

    # ── Init: load candles & instruments ───────────────────────────
    def _init_data(self):
        try:
            log.info("Loading historical candles...")
            end   = datetime.now()
            start = end - timedelta(days=10)

            for sym, tok in [("BANKNIFTY", BANKNIFTY_TOKEN), ("NIFTY", NIFTY_TOKEN)]:
                data = self.kite.historical_data(tok, start, end, "15minute")
                if data:
                    df = pd.DataFrame(data)
                    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
                    df = calc_indicators(df)
                    self._candles[sym] = df.tail(500).to_dict("records")
                    log.info(f"{sym}: {len(self._candles[sym])} candles loaded")

            log.info("Loading NFO instruments...")
            self._nfo_instruments = self.kite.instruments("NFO")
            log.info(f"NFO instruments: {len(self._nfo_instruments)}")

            self.scan_stats["BANKNIFTY"]["status"] = "Scanning..."
            self.scan_stats["NIFTY"]["status"]     = "Scanning..."
            send_telegram(
                "🚀 <b>Friday Dual-Beta Engine Online</b>\n"
                "📊 BankNifty + Nifty Patterns Active\n"
                "⚡ Option-Price Based SL & Targets"
            )
        except Exception as e:
            log.error(f"Init error: {e}")

    # ── Tick handler (called from dashboard ticker) ─────────────────
    def on_tick(self, ticks: list):
        for tick in ticks:
            tok = tick.get("instrument_token")
            ltp = tick.get("last_price", 0)

            if tok == BANKNIFTY_TOKEN:
                self.index_ltp["BANKNIFTY"] = ltp
                self._check_active_trade("BANKNIFTY", ltp)
                self._check_reversal_trigger("BANKNIFTY", ltp)
                self._build_candle("BANKNIFTY", ltp)

            elif tok == NIFTY_TOKEN:
                self.index_ltp["NIFTY"] = ltp
                self._check_active_trade("NIFTY", ltp)
                self._check_reversal_trigger("NIFTY", ltp)
                self._build_candle("NIFTY", ltp)

    # ── 15-min Candle Builder ───────────────────────────────────────
    def _build_candle(self, sym: str, price: float):
        if not is_market_open():
            return

        now    = datetime.now()
        minute = (now.minute // 15) * 15
        ctime  = now.replace(minute=minute, second=0, microsecond=0)
        state  = self._ws_states[sym]

        if state["current_time"] != ctime:
            # Candle closed — store and scan
            if state["current_candle"]:
                self._candles[sym].append(state["current_candle"])
                self._candles[sym] = self._candles[sym][-500:]
                df = pd.DataFrame(self._candles[sym])
                df = calc_indicators(df)
                self._on_candle_close(sym, df)

            state["current_time"]   = ctime
            state["current_candle"] = {
                "date": ctime, "open": price,
                "high": price, "low": price, "close": price,
            }
        else:
            c = state["current_candle"]
            c["high"]  = max(c["high"],  price)
            c["low"]   = min(c["low"],   price)
            c["close"] = price

    def _on_candle_close(self, sym: str, df: pd.DataFrame):
        """Called when 15-min candle closes — run all scanners."""
        last = df.iloc[-1]

        # Candle alert to telegram
        msg = (
            f"📊 <b>{sym} 15M CANDLE</b>\n"
            f"🕒 {last.get('date', '')}\n"
            f"O:{round(last['open'],2)}  H:{round(last['high'],2)}  "
            f"L:{round(last['low'],2)}  C:{round(last['close'],2)}\n"
            f"EMA9:{round(last['EMA9'],1)}  EMA20:{round(last['EMA20'],1)}  "
            f"EMA50:{round(last['EMA50'],1)}"
        )
        send_candle_alert(msg)

        # Run scanners only if no active trade for this symbol
        if sym not in self.active_trades:
            self._scan_friday5(sym, df)
            self._scan_reversal(sym, df)
            self._scan_mean_reversion(sym, df)

        self.scan_stats[sym]["last_scan"] = datetime.now(IST).strftime("%H:%M:%S")
        self.on_scan_update(sym, self.scan_stats[sym])

    # ─────────────────────────────────────────────
    #  FRIDAY5 PATTERN SCANNER
    # ─────────────────────────────────────────────
    def _scan_friday5(self, sym: str, df: pd.DataFrame):
        if not is_market_open() or sym in self.active_trades:
            return

        now = datetime.now(IST).time()
        t1s = datetime.strptime("09:25", "%H:%M").time()
        t1e = datetime.strptime("13:15", "%H:%M").time()
        t2s = datetime.strptime("13:30", "%H:%M").time()
        t2e = datetime.strptime("15:15", "%H:%M").time()
        if not ((t1s <= now <= t1e) or (t2s <= now <= t2e)):
            return

        row   = df.iloc[-1]
        prev  = df.iloc[-2]
        prev2 = df.iloc[-3]

        ema9, ema20, ema50 = row["EMA9"], row["EMA20"], row["EMA50"]
        body, range_       = row["Body"], row["Range"]
        upper, lower       = row["Upper_Wick"], row["Lower_Wick"]
        rsi, atr           = row["RSI"], row["ATR3"]

        is_bn = sym == "BANKNIFTY"

        # ── Filters ──
        if is_bn:
            if atr < 20 or range_ > 200 or abs(ema9 - ema20) < 25: return
            if body < 10: return
            if range_ > 0 and (upper / range_ > 0.35 or lower / range_ > 0.35): return
            if 45 <= rsi <= 55: return
            if abs(row["EMA9"] - prev["EMA9"]) < 5: return
        else:
            if atr < 8 or range_ > 80 or abs(ema9 - ema20) < 10: return
            if body < 5: return
            if range_ > 0 and (upper / range_ > 0.35 or lower / range_ > 0.35): return
            if 45 <= rsi <= 55: return

        signals = []
        step    = 100 if is_bn else 50

        # Pattern2: EMA trend + expansion candle
        p2_min_range = 120 if is_bn else 45
        p2_min_body  = 40  if is_bn else 15
        p2_prev_max  = 100 if is_bn else 35

        if (range_ >= p2_min_range and body >= p2_min_body
                and ema9 > ema20 > ema50
                and prev["Range"] < p2_prev_max
                and row["Range"] > prev["Range"]
                and row["close"] > row["open"]):
            signals.append(("Pattern2", "CE"))

        if (range_ >= p2_min_range and body >= p2_min_body
                and ema9 < ema20 < ema50
                and prev["Range"] < p2_prev_max
                and row["Range"] > prev["Range"]
                and row["close"] < row["open"]):
            signals.append(("Pattern2", "PE"))

        # Pattern2.1: Box breakout
        box_max  = 150 if is_bn else 55
        p21_rng  = 120 if is_bn else 45
        last_5_high = df.iloc[-6:-1]["high"].max()
        last_5_low  = df.iloc[-6:-1]["low"].min()
        box_size    = last_5_high - last_5_low

        if (range_ >= p21_rng and row["close"] > row["open"]
                and box_size < box_max and row["close"] > last_5_high
                and (row["close"] > ema9 or row["close"] > ema20 or row["close"] > ema50)):
            signals.append(("Pattern2.1", "CE"))

        if (range_ >= p21_rng and row["close"] < row["open"]
                and box_size < box_max and row["close"] < last_5_low
                and (row["close"] < ema9 or row["close"] < ema20 or row["close"] < ema50)):
            signals.append(("Pattern2.1", "PE"))

        # Pattern4: Momentum big candle
        p4_rng  = 140 if is_bn else 55
        p4_body = 35  if is_bn else 12

        if range_ >= p4_rng and body >= p4_body and range_ > 0:
            if upper / range_ < 0.25 and lower / range_ < 0.25:
                if ema9 > ema20 > ema50 and row["close"] > row["open"]:
                    signals.append(("Pattern4", "CE"))
                if ema9 < ema20 < ema50 and row["close"] < row["open"]:
                    signals.append(("Pattern4", "PE"))

        # Pattern6_Pro: Higher High / Lower Low breakout
        brk_min = 15 if is_bn else 5
        move_max = 150 if is_bn else 60

        if prev["low"] < prev2["low"] and row["close"] > prev["high"] and ema9 > ema20:
            brk = row["close"] - prev["high"]
            if brk >= brk_min:
                recent_high = df.iloc[-10:-1]["high"].max()
                if (row["close"] >= recent_high
                        and row["Body"] >= row["Range"] * 0.5
                        and row["high"] - row["close"] <= row["Body"]):
                    recent_low = df.iloc[-10:-1]["low"].min()
                    if row["close"] - recent_low <= move_max:
                        signals.append(("Pattern6_Pro", "CE"))

        if prev["high"] > prev2["high"] and row["close"] < prev["low"] and ema9 < ema20:
            brk = prev["low"] - row["close"]
            if brk >= brk_min:
                recent_low = df.iloc[-10:-1]["low"].min()
                if (row["close"] <= recent_low
                        and row["Body"] >= row["Range"] * 0.5
                        and row["close"] - row["low"] <= row["Body"]):
                    recent_high = df.iloc[-10:-1]["high"].max()
                    if recent_high - row["close"] <= move_max:
                        signals.append(("Pattern6_Pro", "PE"))

        # ── Execute best signal ──
        buf = 50 if is_bn else 20
        for pattern, side in signals:
            if sym in self.active_trades:
                break

            entry_index = self.index_ltp[sym]
            if entry_index == 0:
                continue

            # EMA clear path filter
            if side == "CE":
                if entry_index < ema20 < entry_index + buf: continue
                if entry_index < ema50 < entry_index + buf: continue
            else:
                if entry_index > ema20 > entry_index - buf: continue
                if entry_index > ema50 > entry_index - buf: continue

            # Get option
            name_key = "BANKNIFTY" if is_bn else "NIFTY"
            opt_step = 100 if is_bn else 50
            strike = get_atm_strike(entry_index, opt_step)
            opt_sym = get_option_symbol(self._nfo_instruments, strike, side, name_key)
            opt_ltp = get_option_ltp(self.kite, opt_sym)

            if opt_ltp <= 0:
                log.warning(f"Option LTP zero for {opt_sym}, skipping")
                continue

            # SL & Target based on OPTION PREMIUM price
            opt_sl      = is_bn and BN_OPT_SL  or NF_OPT_SL
            opt_t1      = is_bn and BN_OPT_TARGET1 or NF_OPT_TARGET1
            lot_qty     = is_bn and BN_OPT_LOT or NF_OPT_LOT

            sl_price     = round(opt_ltp - opt_sl, 1)
            target_price = round(opt_ltp + opt_t1, 1)

            trade = {
                "symbol":        sym,
                "pattern":       pattern,
                "side":          side,
                "direction":     "CALL" if side == "CE" else "PUT",
                "entry_index":   entry_index,
                "option_symbol": opt_sym,
                "strike":        strike,
                "entry":         opt_ltp,        # option buy price
                "sl":            sl_price,        # option sl
                "target":        target_price,    # option target1
                "peak":          opt_ltp,
                "cur_ltp":       opt_ltp,
                "target_level":  0,
                "open_time":     datetime.now(IST).strftime("%H:%M:%S"),
                "lots":          1,
                "lot_qty":       lot_qty,
                "sym_key":       "bn" if is_bn else "nf",
                "token":         0,
            }
            self.active_trades[sym] = trade
            self.scan_stats[sym]["status"] = f"TRADE {side}"
            self.on_trade_open(sym, trade)

            em = "🔵" if side == "CE" else "🔴"
            send_telegram(
                f"{em} <b>{sym} FRIDAY5 SIGNAL</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Pattern:</b> {pattern}\n"
                f"📊 <b>Side:</b> BUY {side} — {opt_sym}\n"
                f"💰 <b>Option Entry:</b> ₹{opt_ltp}\n"
                f"🛑 <b>Option SL:</b> ₹{sl_price}  (−{opt_sl} pts)\n"
                f"🎯 <b>Option T1:</b> ₹{target_price}  (+{opt_t1} pts)\n"
                f"📈 <b>Index Entry:</b> {entry_index}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
            )
            log.info(f"[{sym}] Trade opened: {pattern} {side} @ opt={opt_ltp}")
            break

    # ─────────────────────────────────────────────
    #  REVERSAL SCANNER
    # ─────────────────────────────────────────────
    def _scan_reversal(self, sym: str, df: pd.DataFrame):
        if not is_market_open():
            return
        if sym in self.active_trades or sym in self._pending_reversal:
            return

        now = datetime.now(IST).time()
        t1s = datetime.strptime("09:25", "%H:%M").time()
        t1e = datetime.strptime("11:30", "%H:%M").time()
        t2s = datetime.strptime("13:30", "%H:%M").time()
        t2e = datetime.strptime("15:15", "%H:%M").time()
        if not ((t1s <= now <= t1e) or (t2s <= now <= t2e)):
            return

        row  = df.iloc[-1]
        ema9, ema20, ema50 = row["EMA9"], row["EMA20"], row["EMA50"]

        real_body  = abs(row["close"] - row["open"])
        real_lower = min(row["open"], row["close"]) - row["low"]
        real_upper = row["high"] - max(row["open"], row["close"])
        range_     = row["high"] - row["low"]

        noise_min = 10 if sym == "BANKNIFTY" else 5
        if real_body < noise_min or range_ == 0:
            return

        hammer = (real_lower >= real_body * 2 and real_upper <= real_body
                  and real_body / range_ >= 0.25)
        star   = (real_upper >= real_body * 2 and real_lower <= real_body
                  and real_body / range_ >= 0.25)

        prev_high = df.iloc[-11:-1]["high"].max()
        prev_low  = df.iloc[-11:-1]["low"].min()
        fall      = prev_high - row["low"]
        rise      = row["high"] - prev_low
        move_min  = 200  # same for both

        temp_side, temp_pattern, temp_entry, temp_sl = None, "", 0, 0

        if hammer and fall >= move_min:
            temp_side, temp_pattern = "CE", "HAMMER REVERSAL"
            temp_entry, temp_sl     = row["high"], row["low"]
        elif star and rise >= move_min:
            temp_side, temp_pattern = "PE", "SHOOTING STAR"
            temp_entry, temp_sl     = row["low"], row["high"]

        if not temp_side:
            return

        buf = 50 if sym == "BANKNIFTY" else 20
        entry = temp_entry
        if temp_side == "CE":
            if (entry < ema9 < entry + buf or entry < ema20 < entry + buf
                    or entry < ema50 < entry + buf):
                return
        else:
            if (entry > ema9 > entry - buf or entry > ema20 > entry - buf
                    or entry > ema50 > entry - buf):
                return

        self._pending_reversal[sym] = {
            "side":    temp_side,
            "pattern": temp_pattern,
            "entry":   temp_entry,
            "sl":      temp_sl,
        }

        lbl = "Above 📈" if temp_side == "CE" else "Below 📉"
        em  = "🔵" if temp_side == "CE" else "🔴"
        send_telegram(
            f"{em} <b>{sym} Reversal Signal</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 Pattern: {temp_pattern}\n"
            f"Side: {temp_side}\n"
            f"Entry {lbl}: {temp_entry}\n"
            f"Index SL Ref: {temp_sl}\n"
            f"⏳ Waiting for breakout..."
        )

    def _check_reversal_trigger(self, sym: str, index_ltp: float):
        """Check if pending reversal has been triggered (on every tick)."""
        pending = self._pending_reversal.get(sym)
        if not pending or sym in self.active_trades:
            return

        side    = pending["side"]
        entry   = pending["entry"]
        triggered = (side == "CE" and index_ltp >= entry) or \
                    (side == "PE" and index_ltp <= entry)

        if not triggered:
            return

        # Clear pending
        del self._pending_reversal[sym]

        # Get option at trigger point
        is_bn   = sym == "BANKNIFTY"
        opt_step = 100 if is_bn else 50
        name_key = "BANKNIFTY" if is_bn else "NIFTY"
        strike  = get_atm_strike(index_ltp, opt_step)
        opt_sym = get_option_symbol(self._nfo_instruments, strike, side, name_key)
        opt_ltp = get_option_ltp(self.kite, opt_sym)

        if opt_ltp <= 0:
            log.warning(f"Reversal trigger: Option LTP zero for {opt_sym}")
            return

        opt_sl  = BN_OPT_SL  if is_bn else NF_OPT_SL
        opt_t1  = BN_OPT_TARGET1 if is_bn else NF_OPT_TARGET1
        lot_qty = BN_OPT_LOT if is_bn else NF_OPT_LOT

        sl_price     = round(opt_ltp - opt_sl, 1)
        target_price = round(opt_ltp + opt_t1, 1)

        trade = {
            "symbol":        sym,
            "pattern":       pending["pattern"],
            "side":          side,
            "direction":     "CALL" if side == "CE" else "PUT",
            "entry_index":   index_ltp,
            "option_symbol": opt_sym,
            "strike":        strike,
            "entry":         opt_ltp,
            "sl":            sl_price,
            "target":        target_price,
            "peak":          opt_ltp,
            "cur_ltp":       opt_ltp,
            "target_level":  0,
            "open_time":     datetime.now(IST).strftime("%H:%M:%S"),
            "lots":          1,
            "lot_qty":       lot_qty,
            "sym_key":       "bn" if is_bn else "nf",
            "token":         0,
        }
        self.active_trades[sym] = trade
        self.scan_stats[sym]["status"] = f"TRADE {side}"
        self.on_trade_open(sym, trade)

        em = "🔵" if side == "CE" else "🔴"
        send_telegram(
            f"🚀 <b>{sym} REVERSAL ACTIVATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 Pattern: {pending['pattern']}\n"
            f"📊 Side: BUY {side} — {opt_sym}\n"
            f"💰 <b>Option Entry:</b> ₹{opt_ltp}\n"
            f"🛑 <b>Option SL:</b> ₹{sl_price}  (−{opt_sl} pts)\n"
            f"🎯 <b>Option T1:</b> ₹{target_price}  (+{opt_t1} pts)\n"
            f"📈 Index Trigger: {index_ltp}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
        )

    # ─────────────────────────────────────────────
    #  MEAN REVERSION
    # ─────────────────────────────────────────────
    def _scan_mean_reversion(self, sym: str, df: pd.DataFrame):
        if not is_market_open() or sym in self.active_trades:
            return
        if sym in self._mr_state:
            return

        row  = df.iloc[-1]
        prev = df.iloc[-2]

        close    = row["close"]
        ema200   = row.get("EMA200", 0) or 0
        rsi      = row["RSI"]
        prev_rsi = prev["RSI"]
        lower    = row.get("BBL", 0) or 0
        upper    = row.get("BBU", 0) or 0
        mid      = row.get("BBM", close) or close

        if ema200 == 0 or lower == 0:
            return

        swing_low  = df.iloc[-10:-1]["low"].min()
        swing_high = df.iloc[-10:-1]["high"].max()
        is_bn      = sym == "BANKNIFTY"
        name       = "BankNifty" if is_bn else "Nifty"

        side = None
        if close < lower and prev_rsi < 30 and rsi > prev_rsi and close > ema200:
            side = "CE"
        elif close > upper and prev_rsi > 70 and rsi < prev_rsi and close < ema200:
            side = "PE"

        if not side:
            return

        opt_step = 100 if is_bn else 50
        name_key = "BANKNIFTY" if is_bn else "NIFTY"
        strike   = get_atm_strike(close, opt_step)
        opt_sym  = get_option_symbol(self._nfo_instruments, strike, side, name_key)
        opt_ltp  = get_option_ltp(self.kite, opt_sym)

        if opt_ltp <= 0:
            return

        opt_sl  = BN_OPT_SL  if is_bn else NF_OPT_SL
        opt_t1  = BN_OPT_TARGET1 if is_bn else NF_OPT_TARGET1
        lot_qty = BN_OPT_LOT if is_bn else NF_OPT_LOT

        sl_price     = round(opt_ltp - opt_sl, 1)
        target_price = round(opt_ltp + opt_t1, 1)

        trade = {
            "symbol":        sym,
            "pattern":       "MEAN REVERSION",
            "side":          side,
            "direction":     "CALL" if side == "CE" else "PUT",
            "entry_index":   close,
            "option_symbol": opt_sym,
            "strike":        strike,
            "entry":         opt_ltp,
            "sl":            sl_price,
            "target":        target_price,
            "peak":          opt_ltp,
            "cur_ltp":       opt_ltp,
            "target_level":  0,
            "open_time":     datetime.now(IST).strftime("%H:%M:%S"),
            "lots":          1,
            "lot_qty":       lot_qty,
            "sym_key":       "bn" if is_bn else "nf",
            "token":         0,
        }
        self.active_trades[sym] = trade
        self._mr_state[sym]     = True
        self.scan_stats[sym]["status"] = f"TRADE {side}"
        self.on_trade_open(sym, trade)

        send_telegram(
            f"🟡 <b>{name} MEAN REVERSION</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 Side: BUY {side} — {opt_sym}\n"
            f"💰 <b>Option Entry:</b> ₹{opt_ltp}\n"
            f"🛑 <b>Option SL:</b> ₹{sl_price}\n"
            f"🎯 <b>Option T1:</b> ₹{target_price}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
        )

    # ─────────────────────────────────────────────
    #  ACTIVE TRADE MONITOR (on every index tick)
    # ─────────────────────────────────────────────
    def _check_active_trade(self, sym: str, index_ltp: float):
        trade = self.active_trades.get(sym)
        if not trade:
            return

        # Fetch live option LTP
        opt_sym = trade.get("option_symbol", "")
        if not opt_sym:
            return

        opt_ltp = get_option_ltp(self.kite, opt_sym)
        if opt_ltp <= 0:
            return

        trade["cur_ltp"] = opt_ltp
        trade["peak"]    = max(trade.get("peak", opt_ltp), opt_ltp)

        entry   = trade["entry"]
        sl      = trade["sl"]
        step    = BN_OPT_TRAIL if sym == "BANKNIFTY" else NF_OPT_TRAIL
        level   = trade["target_level"]

        # ── SL Hit ──
        if opt_ltp <= sl:
            pnl_pts = round(opt_ltp - entry, 1)
            pnl_rs  = round(pnl_pts * trade.get("lot_qty", 1), 0)
            status  = "SL" if level == 0 else "TRAIL_SL"
            em      = "❌" if pnl_pts < 0 else "✅"
            txt     = "Closed in Loss" if pnl_pts < 0 else "Closed in Profit"

            send_telegram(
                f"{em} <b>{sym} {trade['pattern']} EXIT</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 {trade['side']} — {opt_sym}\n"
                f"💰 Entry: ₹{entry}  →  Exit: ₹{opt_ltp}\n"
                f"📉 P&L: {'+' if pnl_pts >= 0 else ''}{pnl_pts} pts  |  "
                f"₹{'+' if pnl_rs >= 0 else ''}{int(pnl_rs)}\n"
                f"🏷 {txt}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
            )
            self._close_trade(sym, opt_ltp, status)
            return

        # ── Target / Trail ──
        new_level = int((opt_ltp - entry) // step)
        if new_level > level:
            for lvl in range(level + 1, new_level + 1):
                new_sl   = round(entry + (lvl - 1) * step, 1)
                new_tgt  = round(entry + (lvl + 1) * step, 1)
                pts_hit  = lvl * step
                trade["sl"]           = new_sl
                trade["target"]       = new_tgt
                trade["target_level"] = lvl

                send_telegram(
                    f"🎯 <b>{sym} {trade['pattern']} Target {lvl} Hit!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📊 {trade['side']} — {opt_sym}\n"
                    f"💰 Option LTP: ₹{opt_ltp}  (+{pts_hit} pts)\n"
                    f"🔒 SL Trailed → ₹{new_sl}  (+{(lvl-1)*step} pts)\n"
                    f"🎯 Next Target: ₹{new_tgt}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
                )

    def _close_trade(self, sym: str, exit_px: float, reason: str):
        trade = self.active_trades.pop(sym, None)
        if not trade:
            return
        lot_qty = trade.get("lot_qty", 1)
        pnl     = round((exit_px - trade["entry"]) * lot_qty, 1)
        record  = {
            "date":      datetime.now(IST).strftime("%Y-%m-%d"),
            "symbol":    sym,
            "pattern":   trade.get("pattern", ""),
            "direction": trade["direction"],
            "opt_sym":   trade["option_symbol"],
            "strike":    trade["strike"],
            "entry":     trade["entry"],
            "exit":      exit_px,
            "pnl":       pnl,
            "lots":      trade["lots"],
            "status":    reason,
        }
        self._save_trade(record)
        self.scan_stats[sym]["status"] = "No Trade"
        self._mr_state.pop(sym, None)
        self.on_trade_close(sym)
        self.on_scan_update(sym, self.scan_stats[sym])

    def _save_trade(self, record: dict):
        trades = []
        if os.path.exists(TRADE_FILE):
            try:
                with open(TRADE_FILE) as f:
                    trades = json.load(f)
            except Exception:
                pass
        trades.append(record)
        with open(TRADE_FILE, "w") as f:
            json.dump(trades, f)
