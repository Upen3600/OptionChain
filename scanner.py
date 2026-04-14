"""
╔══════════════════════════════════════════════════════════════════╗
║   OC CONFLUENCE SCANNER — Core Pattern Engine                    ║
║   5-condition pattern: PCR + MaxPain + OI + IV + BidAsk          ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import time
import json
import logging
import threading
import requests
import pandas as pd
from datetime import datetime, date
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

SYMBOLS = {
    "NIFTY": {
        "index_token": 256265,
        "exchange":    "NFO",
        "lot_size":    50,
        "sl_points":   30,
        "target_step": 50,
        "strike_step": 50,
        "dash_key":    "nf",
    },
    "BANKNIFTY": {
        "index_token": 260105,
        "exchange":    "NFO",
        "lot_size":    15,
        "sl_points":   50,
        "target_step": 50,
        "strike_step": 100,
        "dash_key":    "bn",
    },
    "FINNIFTY": {
        "index_token": 257801,
        "exchange":    "NFO",
        "lot_size":    40,
        "sl_points":   30,
        "target_step": 50,
        "strike_step": 50,
        "dash_key":    "fn",
    },
    "SENSEX": {
        "index_token": 265,
        "exchange":    "BFO",
        "lot_size":    10,
        "sl_points":   100,
        "target_step": 50,
        "strike_step": 100,
        "dash_key":    "sx",
    },
}

PCR_CE_THRESHOLD = 0.8
PCR_PE_THRESHOLD = 1.2
IV_SPIKE_PCT     = 10
OI_REDUCE_PCT    = 5
CHAIN_REFRESH_S  = 5    # REST chain refresh interval


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")


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
#  SCANNER CLASS
# ─────────────────────────────────────────────
class OCSScanner:
    def __init__(self, access_token: str,
                 on_trade_open=None, on_trade_close=None, on_scan_update=None):
        self.kite           = KiteConnect(api_key=API_KEY)
        self.kite.set_access_token(access_token)
        self.access_token   = access_token

        self.on_trade_open  = on_trade_open   or (lambda s, t: None)
        self.on_trade_close = on_trade_close  or (lambda s: None)
        self.on_scan_update = on_scan_update  or (lambda s, d: None)

        # State
        self.index_ltp:    dict = {s: 0.0 for s in SYMBOLS}
        self.active_trades: dict = {}
        self.chain_cache:   dict = {}
        self.option_token_map: dict = {}  # token -> (symbol, CE/PE, strike)
        self.option_prices:    dict = {}  # (sym, side, strike) -> ltp
        self._prev_oi: dict = {}
        self._prev_iv: dict = {}
        self._chain_lock = threading.Lock()
        self.scan_stats: dict = {
            s: {"pcr": 0, "max_pain": 0, "status": "Waiting...",
                "last_scan": "--:--:--", "atm": 0,
                "cond": {"pcr": None, "mp": None, "oi": None, "iv": None, "ba": None}}
            for s in SYMBOLS
        }

        # Build instrument map at startup
        self._build_map()

        # Start chain refresh loop
        threading.Thread(target=self._chain_loop, daemon=True).start()

    def set_token(self, token: str):
        self.access_token = token
        self.kite.set_access_token(token)

    # ── Instrument Map ──────────────────────────────────────────────
    def _build_map(self):
        log.info("Building instrument map...")
        for symbol, cfg in SYMBOLS.items():
            try:
                exchange = cfg["exchange"]
                expiry   = self._nearest_expiry(symbol, exchange)
                instr    = pd.DataFrame(self.kite.instruments(exchange))
                instr    = instr[(instr["name"] == symbol) &
                                 (instr["expiry"].astype(str) == expiry)]
                for _, row in instr.iterrows():
                    tok = int(row["instrument_token"])
                    self.option_token_map[tok] = (
                        symbol, row["instrument_type"], int(row["strike"]))
                log.info(f"  {symbol}: {len(instr)} strikes, expiry {expiry}")
            except Exception as e:
                log.error(f"Map build error [{symbol}]: {e}")
        log.info(f"Token map: {len(self.option_token_map)} option tokens")

    def get_all_tokens(self) -> list:
        """All tokens to subscribe (index + options)."""
        index_tokens = [cfg["index_token"] for cfg in SYMBOLS.values()]
        opt_tokens   = list(self.option_token_map.keys())
        return index_tokens + opt_tokens

    # ── Tick Handler (called from KiteTicker) ───────────────────────
    def on_tick(self, ticks: list):
        for tick in ticks:
            tok = tick.get("instrument_token")
            ltp = tick.get("last_price", 0)

            # Index tick
            for sym, cfg in SYMBOLS.items():
                if tok == cfg["index_token"]:
                    self.index_ltp[sym] = ltp
                    # Monitor active trade on every index tick
                    tr = self.active_trades.get(sym)
                    if tr and tr.get("token"):
                        opt_ltp = self.option_prices.get(
                            (sym, tr["side"], tr["strike"]), tr["entry"])
                        self._monitor(sym, opt_ltp)
                    break

            # Option tick
            if tok in self.option_token_map:
                sym, side, strike = self.option_token_map[tok]
                self.option_prices[(sym, side, strike)] = ltp
                # Update active trade cur_ltp in real-time
                tr = self.active_trades.get(sym)
                if tr and tr.get("side") == side and tr.get("strike") == strike:
                    tr["cur_ltp"] = ltp

    # ── Chain Refresh Loop (REST every 5s) ──────────────────────────
    def _chain_loop(self):
        while True:
            if is_market_open():
                for symbol in SYMBOLS:
                    if symbol in self.active_trades:
                        continue
                    try:
                        self._refresh_chain(symbol)
                        with self._chain_lock:
                            chain = self.chain_cache.get(symbol, pd.DataFrame())
                        ltp = self.index_ltp.get(symbol, 0)
                        if ltp > 0 and not chain.empty:
                            result = self._check_pattern(symbol, ltp, chain)
                            self.scan_stats[symbol]["last_scan"] = (
                                datetime.now(IST).strftime("%H:%M:%S"))
                            if result:
                                self._open_trade(symbol, result)
                            else:
                                self.scan_stats[symbol]["status"] = "Scanning..."
                        self.on_scan_update(symbol, self.scan_stats[symbol])
                    except Exception as e:
                        log.error(f"Chain loop [{symbol}]: {e}")
                        self.scan_stats[symbol]["status"] = "Error"
            time.sleep(CHAIN_REFRESH_S)

    def _refresh_chain(self, symbol: str):
        cfg      = SYMBOLS[symbol]
        exchange = cfg["exchange"]
        expiry   = self._nearest_expiry(symbol, exchange)
        instr    = pd.DataFrame(self.kite.instruments(exchange))
        df       = instr[(instr["name"] == symbol) &
                         (instr["expiry"].astype(str) == expiry)].copy()
        if df.empty:
            return
        tokens  = df["instrument_token"].tolist()
        quotes  = {}
        for i in range(0, len(tokens), 500):
            batch = df[df["instrument_token"].isin(tokens[i:i+500])]
            keys  = [f"{exchange}:{r['tradingsymbol']}" for _, r in batch.iterrows()]
            quotes.update(self.kite.quote(keys))
        rows = []
        for _, row in df.iterrows():
            key = f"{exchange}:{row['tradingsymbol']}"
            if key not in quotes:
                continue
            q   = quotes[key]
            dep = q.get("depth", {})
            rows.append({
                "strike":        int(row["strike"]),
                "option_type":   row["instrument_type"],
                "ltp":           q.get("last_price", 0) or 0,
                "oi":            q.get("oi", 0) or 0,
                "iv":            q.get("implied_volatility", 0) or 0,
                "bid_qty":       sum(b.get("quantity", 0) for b in dep.get("buy",  [])[:5]),
                "ask_qty":       sum(a.get("quantity", 0) for a in dep.get("sell", [])[:5]),
                "token":         int(row["instrument_token"]),
                "tradingsymbol": row["tradingsymbol"],
            })
        if not rows:
            return
        df_oc = pd.DataFrame(rows)
        ce    = df_oc[df_oc["option_type"] == "CE"].set_index("strike").add_prefix("ce_")
        pe    = df_oc[df_oc["option_type"] == "PE"].set_index("strike").add_prefix("pe_")
        chain = ce.join(pe, how="outer").reset_index()
        with self._chain_lock:
            self.chain_cache[symbol] = chain

    # ── Pattern Logic ────────────────────────────────────────────────
    def _check_pattern(self, symbol: str, ltp: float, chain: pd.DataFrame):
        cfg      = SYMBOLS[symbol]
        step     = cfg["strike_step"]
        atm      = int(round(ltp / step) * step)
        pcr      = self._calc_pcr(chain)
        max_pain = self._calc_maxpain(chain)

        self.scan_stats[symbol].update({
            "pcr": round(pcr, 2), "max_pain": max_pain, "atm": atm})

        atm_rows = chain[chain["strike"] == atm]
        if atm_rows.empty:
            atm_rows = chain.iloc[(chain["strike"] - atm).abs().argsort()[:1]]
        row = atm_rows.iloc[0]

        ce_iv  = float(row.get("ce_iv",      0) or 0)
        pe_iv  = float(row.get("pe_iv",      0) or 0)
        ce_oi  = float(row.get("ce_oi",      0) or 0)
        pe_oi  = float(row.get("pe_oi",      0) or 0)
        ce_bid = float(row.get("ce_bid_qty", 0) or 0)
        ce_ask = float(row.get("ce_ask_qty", 0) or 0)
        pe_bid = float(row.get("pe_bid_qty", 0) or 0)
        pe_ask = float(row.get("pe_ask_qty", 0) or 0)

        prev_oi = self._prev_oi.get(symbol, {})
        prev_iv = self._prev_iv.get(symbol, {})
        self._prev_oi[symbol] = {"ce": ce_oi, "pe": pe_oi}
        self._prev_iv[symbol] = {"ce": ce_iv, "pe": pe_iv}

        if not prev_oi:
            return None

        pe_oi_chg = ((pe_oi - prev_oi.get("pe", pe_oi)) / (prev_oi.get("pe", pe_oi) + 1)) * 100
        ce_oi_chg = ((ce_oi - prev_oi.get("ce", ce_oi)) / (prev_oi.get("ce", ce_oi) + 1)) * 100
        ce_iv_spk = ((ce_iv - prev_iv.get("ce", ce_iv)) / (prev_iv.get("ce", ce_iv) + 0.01)) * 100
        pe_iv_spk = ((pe_iv - prev_iv.get("pe", pe_iv)) / (prev_iv.get("pe", pe_iv) + 0.01)) * 100

        # Determine dominant bias for condition display (CE favoured if pcr < 1, else PE)
        if pcr < 1.0:
            cond = {
                "pcr": pcr < PCR_CE_THRESHOLD,
                "mp":  ltp > max_pain,
                "oi":  pe_oi_chg < -OI_REDUCE_PCT,
                "iv":  ce_iv_spk > IV_SPIKE_PCT,
                "ba":  ce_bid > ce_ask,
            }
        else:
            cond = {
                "pcr": pcr > PCR_PE_THRESHOLD,
                "mp":  ltp < max_pain,
                "oi":  ce_oi_chg < -OI_REDUCE_PCT,
                "iv":  pe_iv_spk > IV_SPIKE_PCT,
                "ba":  pe_ask > pe_bid,
            }
        self.scan_stats[symbol]["cond"] = cond

        # CE: all 5 conditions
        if (pcr < PCR_CE_THRESHOLD and ltp > max_pain and
                pe_oi_chg < -OI_REDUCE_PCT and ce_iv_spk > IV_SPIKE_PCT and ce_bid > ce_ask):
            r = chain[chain["strike"] == atm].iloc[0]
            if "ce_tradingsymbol" not in r.index:
                return None
            return {"side": "CE", "strike": atm,
                    "entry": float(r.get("ce_ltp", 0)),
                    "option_symbol": r["ce_tradingsymbol"],
                    "token": int(r.get("ce_token", 0))}

        # PE: all 5 conditions
        if (pcr > PCR_PE_THRESHOLD and ltp < max_pain and
                ce_oi_chg < -OI_REDUCE_PCT and pe_iv_spk > IV_SPIKE_PCT and pe_ask > pe_bid):
            r = chain[chain["strike"] == atm].iloc[0]
            if "pe_tradingsymbol" not in r.index:
                return None
            return {"side": "PE", "strike": atm,
                    "entry": float(r.get("pe_ltp", 0)),
                    "option_symbol": r["pe_tradingsymbol"],
                    "token": int(r.get("pe_token", 0))}

        return None

    # ── Trade Open ───────────────────────────────────────────────────
    def _open_trade(self, symbol: str, result: dict):
        cfg      = SYMBOLS[symbol]
        entry_px = result["entry"]
        trade    = {
            "side":          result["side"],
            "entry":         entry_px,
            "sl":            round(entry_px - cfg["sl_points"], 1),
            "target_level":  0,
            "peak":          entry_px,
            "cur_ltp":       entry_px,
            "option_symbol": result["option_symbol"],
            "token":         result["token"],
            "strike":        result["strike"],
            "open_time":     datetime.now(IST).strftime("%H:%M:%S"),
            "symbol":        symbol,
            "direction":     "CALL" if result["side"] == "CE" else "PUT",
            # For dashboard
            "target": round(entry_px + cfg["target_step"], 1),
            "lots":   1,
            "sym_key": cfg["dash_key"],
        }
        self.active_trades[symbol] = trade
        self.scan_stats[symbol]["status"] = f"TRADE {result['side']}"
        self.on_trade_open(symbol, trade)
        self._alert_entry(symbol, trade)
        log.info(f"[{symbol}] Trade opened {result['side']} @ {entry_px}")

    # ── Trade Monitor ────────────────────────────────────────────────
    def _monitor(self, symbol: str, opt_ltp: float):
        trade = self.active_trades.get(symbol)
        if not trade:
            return
        cfg   = SYMBOLS[symbol]
        entry = trade["entry"]
        sl    = trade["sl"]
        step  = cfg["target_step"]
        level = trade["target_level"]

        trade["peak"]    = max(trade.get("peak", opt_ltp), opt_ltp)
        trade["cur_ltp"] = opt_ltp

        if opt_ltp <= sl:
            if level == 0:
                self._alert_sl_hit(symbol, trade, opt_ltp)
            else:
                self._alert_trail_hit(symbol, trade, opt_ltp)
            self._close_trade(symbol, opt_ltp, "SL" if level == 0 else "TRAIL_SL")
            return

        new_level = int((opt_ltp - entry) // step)
        if new_level > level:
            for lvl in range(level + 1, new_level + 1):
                new_sl = round(entry + (lvl - 1) * step, 1)
                trade["sl"]           = new_sl
                trade["target_level"] = lvl
                trade["target"]       = round(entry + (lvl + 1) * step, 1)
                self._alert_target(symbol, trade, lvl, opt_ltp, new_sl)

    def _close_trade(self, symbol: str, exit_px: float, reason: str):
        trade = self.active_trades.pop(symbol, None)
        if not trade:
            return
        cfg    = SYMBOLS[symbol]
        pnl    = round((exit_px - trade["entry"]) * cfg["lot_size"], 1)
        record = {
            "date":      datetime.now(IST).strftime("%Y-%m-%d"),
            "symbol":    symbol,
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
        self.scan_stats[symbol]["status"] = "No Trade"
        self.on_trade_close(symbol)
        self.on_scan_update(symbol, self.scan_stats[symbol])

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

    # ── Utilities ────────────────────────────────────────────────────
    def _nearest_expiry(self, symbol: str, exchange: str) -> str:
        df = pd.DataFrame(self.kite.instruments(exchange))
        df = df[df["name"] == symbol].copy()
        df["expiry"] = pd.to_datetime(df["expiry"])
        df = df[df["expiry"] >= pd.Timestamp(date.today())]
        return df["expiry"].min().strftime("%Y-%m-%d")

    @staticmethod
    def _calc_pcr(chain: pd.DataFrame) -> float:
        ce = chain["ce_oi"].sum()
        pe = chain["pe_oi"].sum()
        return pe / ce if ce else 0.0

    @staticmethod
    def _calc_maxpain(chain: pd.DataFrame) -> float:
        mp, ml = 0, float("inf")
        for s in sorted(chain["strike"].dropna().unique()):
            loss = (((chain["strike"] - s).clip(lower=0) * chain["ce_oi"]) +
                    ((s - chain["strike"]).clip(lower=0) * chain["pe_oi"])).sum()
            if loss < ml:
                ml, mp = loss, s
        return float(mp)

    # ── Alert Messages ───────────────────────────────────────────────
    def _alert_entry(self, symbol, trade):
        cfg  = SYMBOLS[symbol]
        e, sl, t1 = trade["entry"], trade["sl"], trade["target"]
        em   = "🟢" if trade["side"] == "CE" else "🔴"
        send_telegram(
            f"{em} <b>OC Confluence Trigger — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Pattern:</b> OC Confluence Trigger\n"
            f"📊 <b>Side:</b> {trade['side']} — {trade['option_symbol']}\n"
            f"💰 <b>Entry:</b> ₹{e}\n"
            f"🛑 <b>Initial SL:</b> ₹{sl}  (−{cfg['sl_points']} pts)\n"
            f"🎯 <b>Target 1:</b> ₹{t1}  (+{cfg['target_step']} pts)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
        )

    def _alert_sl_hit(self, symbol, trade, ltp):
        loss = round(trade["entry"] - ltp, 1)
        send_telegram(
            f"❌ <b>OC Confluence Trigger — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🚨 <b>Pattern:</b> Exit — Initial SL Hit\n"
            f"📊 <b>Side:</b> {trade['side']} — {trade['option_symbol']}\n"
            f"📉 <b>Trade Closed in Loss:</b> −{loss} pts\n"
            f"💸 LTP: ₹{ltp}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
        )

    def _alert_target(self, symbol, trade, level, ltp, new_sl):
        cfg = SYMBOLS[symbol]
        pts = level * cfg["target_step"]
        send_telegram(
            f"🎯 <b>OC Confluence Trigger — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>Target {level} Hit  +{pts} pts</b>\n"
            f"📊 <b>Side:</b> {trade['side']} — {trade['option_symbol']}\n"
            f"📈 <b>LTP:</b> ₹{ltp}\n"
            f"🔒 <b>SL Trailed To:</b> ₹{new_sl}  (+{(level-1)*cfg['target_step']} pts)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
        )

    def _alert_trail_hit(self, symbol, trade, ltp):
        profit = round(ltp - trade["entry"], 1)
        send_telegram(
            f"✅ <b>OC Confluence Trigger — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏁 <b>Trade Closed — Trailing SL Hit</b>\n"
            f"📊 <b>Side:</b> {trade['side']} — {trade['option_symbol']}\n"
            f"💚 <b>Trade Closed in Profit: +{profit} pts</b>\n"
            f"Exit LTP: ₹{ltp}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}"
        )
