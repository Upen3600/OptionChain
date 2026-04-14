"""
╔══════════════════════════════════════════════════════════════════╗
║   OC CONFLUENCE DASHBOARD — Flask + SocketIO                     ║
║   Tick-by-Tick LTP | Scan Status | Live Trade P&L                ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ── Gevent monkey-patch MUST be first ──
from gevent import monkey
monkey.patch_all()

import os
import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, render_template_string
from flask_socketio import SocketIO
from kiteconnect import KiteTicker

IST        = ZoneInfo("Asia/Kolkata")
log        = logging.getLogger(__name__)
PORT       = int(os.environ.get("PORT", 8080))
API_KEY    = os.environ.get("API_KEY", "yj3cey9o0ho0gi1b")
TRADE_FILE = "/tmp/trades_log.json"

SYMBOL_TOKENS = {
    256265: {"key": "nf",  "name": "NIFTY"},
    260105: {"key": "bn",  "name": "BANKNIFTY"},
    257801: {"key": "fn",  "name": "FINNIFTY"},
    265:    {"key": "sx",  "name": "SENSEX"},
}

app      = Flask(__name__)
app.config["SECRET_KEY"] = "oc_scanner_2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# ── Shared state ──
_tick_data = {
    info["key"]: {
        "name": info["name"], "ltp": 0, "high": 0, "low": 0,
        "close": 0, "open": 0, "change": 0, "change_pct": 0,
        "volume": 0, "time": "--:--:--",
        "status": "Waiting...", "pcr": 0, "max_pain": 0, "atm": 0,
        "last_scan": "--:--:--",
        "cond": {"pcr": None, "mp": None, "oi": None, "iv": None, "ba": None},
    }
    for info in SYMBOL_TOKENS.values()
}
_active_trades: dict = {}
_ticker = None
_scanner_ref = None


def set_active_trade(symbol: str, trade_data):
    """Called by scanner when trade opens/closes."""
    key = next((v["key"] for k, v in SYMBOL_TOKENS.items()
                if v["name"] == symbol), None)
    if not key:
        return
    if trade_data:
        _active_trades[key] = {**trade_data, "sym_key": key}
    else:
        _active_trades.pop(key, None)
    socketio.emit("active_trade", {
        "active": list(_active_trades.values())[0]
        if _active_trades else None
    })


def push_scan_update(symbol: str, data: dict):
    """Called by scanner after each chain check."""
    key = next((v["key"] for k, v in SYMBOL_TOKENS.items()
                if v["name"] == symbol), None)
    if not key:
        return
    _tick_data[key].update({
        "status":    data.get("status", ""),
        "pcr":       data.get("pcr", 0),
        "max_pain":  data.get("max_pain", 0),
        "atm":       data.get("atm", 0),
        "last_scan": data.get("last_scan", "--:--:--"),
        "cond":      data.get("cond", {"pcr": None, "mp": None, "oi": None, "iv": None, "ba": None}),
    })
    socketio.emit("scan_update", {"sym": key, **_tick_data[key]})


# ─────────────────────────────────────────────
#  KITE TICKER
# ─────────────────────────────────────────────
def start_ticker(access_token: str, scanner=None):
    global _ticker, _scanner_ref
    _scanner_ref = scanner
    if _ticker:
        try: _ticker.stop()
        except Exception: pass

    _ticker = KiteTicker(API_KEY, access_token)

    def on_ticks(ws, ticks):
        for tick in ticks:
            tok  = tick.get("instrument_token")
            info = SYMBOL_TOKENS.get(tok)
            if not info:
                if _scanner_ref:
                    _scanner_ref.on_tick([tick])
                continue

            key   = info["key"]
            ltp   = tick.get("last_price", 0)
            ohlc  = tick.get("ohlc", {})
            high  = ohlc.get("high",  ltp)
            low   = ohlc.get("low",   ltp)
            close = ohlc.get("close", ltp)
            opn   = ohlc.get("open",  ltp)
            vol   = tick.get("volume_traded", 0)
            chg   = round(ltp - close, 2) if close else 0
            chgp  = round((chg / close) * 100, 2) if close else 0
            now_s = datetime.now(IST).strftime("%H:%M:%S")

            _tick_data[key].update({
                "ltp": round(ltp, 2), "high": round(high, 2),
                "low": round(low, 2), "close": round(close, 2),
                "open": round(opn, 2), "volume": vol,
                "change": chg, "change_pct": chgp, "time": now_s,
            })

            # Also update active trade cur_ltp if it's for this symbol
            # (option ticks update cur_ltp directly via scanner.on_tick)

            socketio.emit("tick", {
                "sym": key, "ltp": round(ltp, 2),
                "high": round(high, 2), "low": round(low, 2),
                "close": round(close, 2), "change": chg,
                "change_pct": chgp, "volume": vol, "time": now_s,
            })

            if _scanner_ref:
                _scanner_ref.on_tick([tick])

    def on_connect(ws, response):
        log.info("KiteTicker connected.")
        all_tokens  = list(SYMBOL_TOKENS.keys())
        index_tokens = list(SYMBOL_TOKENS.keys())
        if _scanner_ref:
            all_tokens += _scanner_ref.get_all_tokens()
        opt_tokens = [t for t in all_tokens if t not in index_tokens]

        ws.subscribe(index_tokens)
        ws.set_mode(ws.MODE_FULL, index_tokens)
        if opt_tokens:
            for i in range(0, len(opt_tokens), 1000):
                batch = opt_tokens[i:i+1000]
                ws.subscribe(batch)
                ws.set_mode(ws.MODE_LTP, batch)
        socketio.emit("ws_status", {"connected": True})

    def on_disconnect(ws, code, reason):
        log.warning(f"KiteTicker disconnected: {code}")
        socketio.emit("ws_status", {"connected": False})

    def on_error(ws, code, reason):
        log.error(f"KiteTicker error: {code} {reason}")

    _ticker.on_ticks      = on_ticks
    _ticker.on_connect    = on_connect
    _ticker.on_disconnect = on_disconnect
    _ticker.on_error      = on_error
    _ticker.connect(threaded=True)


# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/snapshot")
def api_snapshot():
    trades = []
    if os.path.exists(TRADE_FILE):
        try:
            with open(TRADE_FILE) as f:
                trades = json.load(f)
        except Exception:
            pass
    active = None
    if _active_trades:
        a = list(_active_trades.values())[0]
        sym = a.get("symbol")
        tr  = _scanner_ref.active_trades.get(sym) if _scanner_ref else None
        active = {**a, "cur_ltp": tr["cur_ltp"] if tr else a.get("cur_ltp", a.get("entry", 0))}
    return {"market": _tick_data, "trades": trades, "active": active}

@app.route("/api/trades")
def api_trades():
    if os.path.exists(TRADE_FILE):
        try:
            with open(TRADE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

@app.route("/api/active")
def api_active():
    if not _active_trades:
        return {"active": None}
    a   = list(_active_trades.values())[0]
    sym = a.get("symbol")
    tr  = _scanner_ref.active_trades.get(sym) if _scanner_ref else None
    cur_ltp = tr["cur_ltp"] if tr else a.get("cur_ltp", a.get("entry", 0))
    return {"active": {**a, "cur_ltp": cur_ltp}}

@socketio.on("connect")
def on_client_connect():
    trades = []
    if os.path.exists(TRADE_FILE):
        try:
            with open(TRADE_FILE) as f:
                trades = json.load(f)
        except Exception:
            pass
    active = None
    if _active_trades:
        a   = list(_active_trades.values())[0]
        sym = a.get("symbol")
        tr  = _scanner_ref.active_trades.get(sym) if _scanner_ref else None
        active = {**a, "cur_ltp": tr["cur_ltp"] if tr else a.get("cur_ltp", a.get("entry", 0))}
    socketio.emit("snapshot", {
        "market": _tick_data, "trades": trades, "active": active
    })


# ─────────────────────────────────────────────
#  HTML DASHBOARD
# ─────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OC Confluence Scanner — Live Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0b0d14;color:#e2e8f0;min-height:100vh}
header{background:#12151f;border-bottom:1px solid #1e2535;padding:12px 20px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.logo{font-size:15px;font-weight:700;color:#fff;letter-spacing:.5px}
.pills{display:flex;gap:8px;align-items:center}
.pill{display:flex;align-items:center;gap:5px;background:#1a2035;
  border:1px solid #2d3748;border-radius:20px;padding:3px 10px;font-size:11px;color:#a0aec0}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot-g{background:#48bb78;box-shadow:0 0 5px #48bb7880;animation:pulse 1.5s infinite}
.dot-r{background:#fc8181}
.dot-y{background:#f6c90e;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.clock-box{text-align:right}
.clock-t{font-size:20px;font-weight:800;color:#fff;letter-spacing:1.5px;font-variant-numeric:tabular-nums}
.clock-d{font-size:11px;color:#4a5568;margin-top:2px}
.container{max-width:1280px;margin:0 auto;padding:18px 14px}
.section-label{font-size:10px;color:#4a5568;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px;padding-left:2px}

/* ── Market Grid ── */
.mgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}
@media(max-width:900px){.mgrid{grid-template-columns:1fr 1fr}}
@media(max-width:520px){.mgrid{grid-template-columns:1fr}}

.mcard{background:#12151f;border:1px solid #1e2535;border-radius:14px;padding:14px 16px;position:relative}
.mcard.nf{border-top:3px solid #f5576c}
.mcard.bn{border-top:3px solid #667eea}
.mcard.fn{border-top:3px solid #f6c90e}
.mcard.sx{border-top:3px solid #48bb78}

.mc-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.mc-name{font-size:10px;font-weight:700;color:#a0aec0;letter-spacing:1px}
.mc-badge{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:700}
.bo{background:#1a3a2a;color:#48bb78}
.bc{background:#2a1a1a;color:#fc8181}
.bp{background:#2a2a1a;color:#f6c90e}

.ltp{font-size:26px;font-weight:900;color:#fff;font-variant-numeric:tabular-nums;
  letter-spacing:-.5px;transition:color .15s;display:block;margin-bottom:4px}
.ltp.flash-up{color:#48bb78 !important;text-shadow:0 0 10px #48bb7850}
.ltp.flash-dn{color:#fc8181 !important;text-shadow:0 0 10px #fc818150}

.chg-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.chg{font-size:12px;font-weight:700;padding:2px 7px;border-radius:6px}
.cup{background:#1a3a2a;color:#48bb78}.cdn{background:#3a1a1a;color:#fc8181}.cfl{background:#1e2535;color:#718096}
.tick-time{font-size:10px;color:#4a5568}

.hl-row{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px}
.hl-box{background:#0f1117;border-radius:7px;padding:6px 8px}
.hl-lbl{font-size:9px;color:#4a5568;text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px}
.hl-v{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}
.hv-h{color:#48bb78}.hv-l{color:#fc8181}

/* Scan info row */
.scan-row{border-top:1px solid #1e2535;padding-top:8px;margin-top:4px}
.scan-item{display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px}
.scan-lbl{color:#4a5568}
.scan-val{font-weight:600;font-variant-numeric:tabular-nums}
.status-scan{color:#f6c90e}
.status-trade-ce{color:#48bb78;font-weight:700}
.status-trade-pe{color:#fc8181;font-weight:700}
.status-err{color:#fc8181}
.status-wait{color:#4a5568}

/* ── Conditions Bar ── */
.cond-bar{display:flex;gap:4px;margin-top:7px;flex-wrap:wrap}
.cond-pill{display:flex;align-items:center;gap:3px;font-size:9px;font-weight:700;
  padding:2px 6px;border-radius:8px;letter-spacing:.3px;border:1px solid transparent}
.cond-pass{background:#1a3a2a;color:#48bb78;border-color:#2d6a4a}
.cond-fail{background:#3a1a1a;color:#fc8181;border-color:#6a2d2d}
.cond-wait{background:#1a1d2e;color:#4a5568;border-color:#2d3748}
.cond-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.cond-dot-g{background:#48bb78}
.cond-dot-r{background:#fc8181}
.cond-dot-y{background:#4a5568}

/* ── Active Trade ── */
.atrade-wrap{margin-bottom:20px}
.atrade{background:#12151f;border:2px solid #f6c90e55;border-radius:14px;padding:16px 18px}
.atrade-none{background:#12151f;border:1px solid #1e2535;border-radius:14px;padding:14px 18px;
  text-align:center;color:#4a5568;font-size:13px}
.at-title{font-size:10px;color:#f6c90e;text-transform:uppercase;letter-spacing:1px;
  margin-bottom:10px;display:flex;align-items:center;gap:6px}
.at-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.at-box{background:#0f1117;border-radius:8px;padding:8px 10px}
.at-lbl{font-size:9px;color:#4a5568;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.at-val{font-size:14px;font-weight:800;font-variant-numeric:tabular-nums}
.at-pnl{font-size:22px;font-weight:900;font-variant-numeric:tabular-nums}

/* ── Trade Table ── */
.panel{background:#12151f;border:1px solid #1e2535;border-radius:12px;padding:14px;margin-bottom:18px}
.twrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:560px}
th{text-align:left;padding:7px 9px;color:#4a5568;font-weight:500;
  border-bottom:1px solid #1e2535;font-size:9px;text-transform:uppercase;letter-spacing:.6px}
td{padding:8px 9px;border-bottom:1px solid #0f1117;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}tr:hover td{background:#1a1d2e}
.b{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10px;font-weight:700}
.bg2{background:#1a3a2a;color:#48bb78}.br2{background:#3a1a1a;color:#fc8181}
.by2{background:#3a3010;color:#f6c90e}.bb2{background:#1a2a3a;color:#63b3ed}
.nodata{text-align:center;color:#2d3748;padding:24px;font-size:13px}
.footer{text-align:center;font-size:10px;color:#2d3748;padding-bottom:20px}
</style>
</head>
<body>

<header>
  <div class="pills">
    <span class="logo">⚡ OC Confluence Scanner</span>
    <div class="pill"><span class="dot dot-y" id="ws-dot"></span><span id="ws-txt">Connecting...</span></div>
    <div class="pill"><span class="dot dot-g"></span><span>Live Dashboard</span></div>
  </div>
  <div class="clock-box">
    <div class="clock-t" id="clock">--:--:--</div>
    <div class="clock-d" id="cdate">Asia / Kolkata</div>
  </div>
</header>

<div class="container">

  <!-- Market Cards -->
  <div class="section-label">Live Market — Tick by Tick</div>
  <div class="mgrid">

    <div class="mcard nf" id="card-nf">
      <div class="mc-head"><span class="mc-name">NIFTY 50</span><span class="mc-badge bp" id="nf-badge">—</span></div>
      <span class="ltp" id="nf-ltp">—</span>
      <div class="chg-row"><span class="chg cfl" id="nf-chg">—</span><span class="tick-time" id="nf-time">—</span></div>
      <div class="hl-row">
        <div class="hl-box"><div class="hl-lbl">Day High</div><div class="hl-v hv-h" id="nf-high">—</div></div>
        <div class="hl-box"><div class="hl-lbl">Day Low</div><div class="hl-v hv-l" id="nf-low">—</div></div>
      </div>
      <div class="scan-row">
        <div class="scan-item"><span class="scan-lbl">PCR</span><span class="scan-val" id="nf-pcr">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Max Pain</span><span class="scan-val" id="nf-mp">—</span></div>
        <div class="scan-item"><span class="scan-lbl">ATM</span><span class="scan-val" id="nf-atm">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Status</span><span class="scan-val" id="nf-status">Waiting...</span></div>
        <div class="scan-item"><span class="scan-lbl">Last Scan</span><span class="scan-val" style="color:#4a5568" id="nf-scan-ts">—</span></div>
        <div class="cond-bar" id="nf-conds"></div>
      </div>
    </div>

    <div class="mcard bn" id="card-bn">
      <div class="mc-head"><span class="mc-name">BANK NIFTY</span><span class="mc-badge bp" id="bn-badge">—</span></div>
      <span class="ltp" id="bn-ltp">—</span>
      <div class="chg-row"><span class="chg cfl" id="bn-chg">—</span><span class="tick-time" id="bn-time">—</span></div>
      <div class="hl-row">
        <div class="hl-box"><div class="hl-lbl">Day High</div><div class="hl-v hv-h" id="bn-high">—</div></div>
        <div class="hl-box"><div class="hl-lbl">Day Low</div><div class="hl-v hv-l" id="bn-low">—</div></div>
      </div>
      <div class="scan-row">
        <div class="scan-item"><span class="scan-lbl">PCR</span><span class="scan-val" id="bn-pcr">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Max Pain</span><span class="scan-val" id="bn-mp">—</span></div>
        <div class="scan-item"><span class="scan-lbl">ATM</span><span class="scan-val" id="bn-atm">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Status</span><span class="scan-val" id="bn-status">Waiting...</span></div>
        <div class="scan-item"><span class="scan-lbl">Last Scan</span><span class="scan-val" style="color:#4a5568" id="bn-scan-ts">—</span></div>
        <div class="cond-bar" id="bn-conds"></div>
      </div>
    </div>

    <div class="mcard fn" id="card-fn">
      <div class="mc-head"><span class="mc-name">FIN NIFTY</span><span class="mc-badge bp" id="fn-badge">—</span></div>
      <span class="ltp" id="fn-ltp">—</span>
      <div class="chg-row"><span class="chg cfl" id="fn-chg">—</span><span class="tick-time" id="fn-time">—</span></div>
      <div class="hl-row">
        <div class="hl-box"><div class="hl-lbl">Day High</div><div class="hl-v hv-h" id="fn-high">—</div></div>
        <div class="hl-box"><div class="hl-lbl">Day Low</div><div class="hl-v hv-l" id="fn-low">—</div></div>
      </div>
      <div class="scan-row">
        <div class="scan-item"><span class="scan-lbl">PCR</span><span class="scan-val" id="fn-pcr">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Max Pain</span><span class="scan-val" id="fn-mp">—</span></div>
        <div class="scan-item"><span class="scan-lbl">ATM</span><span class="scan-val" id="fn-atm">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Status</span><span class="scan-val" id="fn-status">Waiting...</span></div>
        <div class="scan-item"><span class="scan-lbl">Last Scan</span><span class="scan-val" style="color:#4a5568" id="fn-scan-ts">—</span></div>
        <div class="cond-bar" id="fn-conds"></div>
      </div>
    </div>

    <div class="mcard sx" id="card-sx">
      <div class="mc-head"><span class="mc-name">SENSEX</span><span class="mc-badge bp" id="sx-badge">—</span></div>
      <span class="ltp" id="sx-ltp">—</span>
      <div class="chg-row"><span class="chg cfl" id="sx-chg">—</span><span class="tick-time" id="sx-time">—</span></div>
      <div class="hl-row">
        <div class="hl-box"><div class="hl-lbl">Day High</div><div class="hl-v hv-h" id="sx-high">—</div></div>
        <div class="hl-box"><div class="hl-lbl">Day Low</div><div class="hl-v hv-l" id="sx-low">—</div></div>
      </div>
      <div class="scan-row">
        <div class="scan-item"><span class="scan-lbl">PCR</span><span class="scan-val" id="sx-pcr">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Max Pain</span><span class="scan-val" id="sx-mp">—</span></div>
        <div class="scan-item"><span class="scan-lbl">ATM</span><span class="scan-val" id="sx-atm">—</span></div>
        <div class="scan-item"><span class="scan-lbl">Status</span><span class="scan-val" id="sx-status">Waiting...</span></div>
        <div class="scan-item"><span class="scan-lbl">Last Scan</span><span class="scan-val" style="color:#4a5568" id="sx-scan-ts">—</span></div>
        <div class="cond-bar" id="sx-conds"></div>
      </div>
    </div>

  </div>

  <!-- Active Trade -->
  <div class="section-label">Active Position</div>
  <div class="atrade-wrap">
    <div id="active-panel" class="atrade-none">No open position</div>
  </div>

  <!-- Trade History -->
  <div class="section-label">Trade History</div>
  <div class="panel">
    <div class="twrap" id="ttable"><div class="nodata">No trades yet</div></div>
  </div>

  <div class="footer">OC Confluence Scanner · Tick-by-Tick WebSocket · Zerodha Kite</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.6.1/socket.io.min.js"></script>
<script>
// ── Clock ──
function tickClock(){
  const ist=new Date(new Date().toLocaleString("en-US",{timeZone:"Asia/Kolkata"}));
  document.getElementById('clock').textContent=
    ist.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  document.getElementById('cdate').textContent=
    ist.toLocaleDateString('en-IN',{weekday:'short',day:'2-digit',month:'short',year:'numeric'});
  const h=ist.getHours(),m=ist.getMinutes(),d=ist.getDay();
  const mins=h*60+m;
  const open=d>0&&d<6&&mins>=555&&mins<930;
  ['nf','bn','fn','sx'].forEach(s=>{
    const b=document.getElementById(s+'-badge');
    if(!b)return;
    if(open){b.textContent='LIVE';b.className='mc-badge bo';}
    else{b.textContent='CLOSED';b.className='mc-badge bc';}
  });
}
setInterval(tickClock,1000);tickClock();

// ── Helpers ──
function fn(n,d=2){return n==null||isNaN(n)?'—':Number(n).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:d});}
function fp(n){if(n==null||isNaN(n))return'—';const s=n<0?'-':'+';return s+'₹'+Math.abs(n).toLocaleString('en-IN',{maximumFractionDigits:0});}

const prevLtp={nf:0,bn:0,fn:0,sx:0};
// option LTP cache keyed by sym_key — updated from active_trade events
const optLtp={nf:0,bn:0,fn:0,sx:0};

// ── Tick Update ──
function applyTick(d){
  const s=d.sym;
  const el=document.getElementById(s+'-ltp');
  if(!el)return;
  const prev=prevLtp[s],ltp=d.ltp;
  if(prev&&ltp!==prev){
    el.classList.remove('flash-up','flash-dn');
    void el.offsetWidth;
    el.classList.add(ltp>prev?'flash-up':'flash-dn');
    setTimeout(()=>el.classList.remove('flash-up','flash-dn'),400);
  }
  prevLtp[s]=ltp;
  el.textContent=fn(ltp);
  const h=document.getElementById(s+'-high');
  const l=document.getElementById(s+'-low');
  const t=document.getElementById(s+'-time');
  if(h)h.textContent=fn(d.high);
  if(l)l.textContent=fn(d.low);
  if(t)t.textContent=d.time||'';
  const chg=d.change||0,chgp=d.change_pct||0;
  const cEl=document.getElementById(s+'-chg');
  if(cEl){
    cEl.textContent=(chg>=0?'+':'')+fn(chg)+' ('+(chgp>=0?'+':'')+fn(chgp)+'%)';
    cEl.className='chg '+(chg>0?'cup':chg<0?'cdn':'cfl');
  }
}

// ── Conditions Bar ──
const COND_LABELS={pcr:'PCR',mp:'MaxPain',oi:'OI↓',iv:'IV↑',ba:'Bid>Ask'};
function applyCondBar(s, cond){
  const el=document.getElementById(s+'-conds');
  if(!el||!cond)return;
  let html='';
  Object.entries(COND_LABELS).forEach(([k,label])=>{
    const v=cond[k];
    let cls='cond-wait',dc='cond-dot-y';
    if(v===true){cls='cond-pass';dc='cond-dot-g';}
    else if(v===false){cls='cond-fail';dc='cond-dot-r';}
    html+=`<span class="cond-pill ${cls}"><span class="cond-dot ${dc}"></span>${label}</span>`;
  });
  el.innerHTML=html;
}

// ── Scan Update ──
function applyScan(d){
  const s=d.sym;
  const pcr=document.getElementById(s+'-pcr');
  const mp=document.getElementById(s+'-mp');
  const atm=document.getElementById(s+'-atm');
  const sts=document.getElementById(s+'-status');
  const ts=document.getElementById(s+'-scan-ts');
  if(pcr)pcr.textContent=d.pcr?Number(d.pcr).toFixed(2):'—';
  if(mp)mp.textContent=d.max_pain?'₹'+Number(d.max_pain).toLocaleString('en-IN'):'—';
  if(atm)atm.textContent=d.atm||'—';
  if(ts)ts.textContent=d.last_scan||'—';
  if(sts){
    const st=d.status||'';
    sts.textContent=st;
    sts.className='scan-val '+
      (st.includes('TRADE CE')?'status-trade-ce':
       st.includes('TRADE PE')?'status-trade-pe':
       st.includes('Scan')?'status-scan':
       st.includes('Error')?'status-err':'status-wait');
  }
  if(d.cond) applyCondBar(s, d.cond);
}

// ── Active Trade — uses cur_ltp from scanner (option LTP) ──
function renderActive(a){
  const panel=document.getElementById('active-panel');
  if(!a){
    panel.className='atrade-none';
    panel.innerHTML='No open position';
    return;
  }
  // cur_ltp is the option's live LTP from scanner; fallback to entry
  const ltp = (a.cur_ltp && a.cur_ltp > 0) ? a.cur_ltp : (a.entry||0);
  const entry=a.entry||0;
  const lotSize=(a.symbol==='BANKNIFTY'?15:a.symbol==='SENSEX'?10:a.symbol==='FINNIFTY'?40:50);
  const pnl=(ltp-entry)*lotSize;
  const pct=entry?(ltp-entry)/entry*100:0;
  const pc=pnl>=0?'#48bb78':'#fc8181';
  const dc=a.direction==='CALL'?'#48bb78':'#fc8181';
  panel.className='atrade';
  panel.innerHTML=`
    <div class="at-title">
      <span class="dot ${pnl>=0?'dot-g':'dot-r'}"></span>
      LIVE TRADE — ${a.symbol||''}
    </div>
    <div class="at-grid">
      <div class="at-box"><div class="at-lbl">Option</div><div class="at-val" style="font-size:11px;color:#a0aec0">${a.option_symbol||'—'}</div></div>
      <div class="at-box"><div class="at-lbl">Side</div><div class="at-val" style="color:${dc}">${a.side||'—'}</div></div>
      <div class="at-box"><div class="at-lbl">Entry</div><div class="at-val">₹${(entry).toFixed(2)}</div></div>
      <div class="at-box"><div class="at-lbl">Live LTP</div><div class="at-val" id="at-live-ltp" style="color:${ltp>=entry?'#48bb78':'#fc8181'}">₹${ltp.toFixed(2)}</div></div>
      <div class="at-box"><div class="at-lbl">SL</div><div class="at-val" style="color:#fc8181">₹${(a.sl||0).toFixed(2)}</div></div>
      <div class="at-box"><div class="at-lbl">Target</div><div class="at-val" style="color:#48bb78">₹${(a.target||0).toFixed(2)}</div></div>
      <div class="at-box"><div class="at-lbl">Opened</div><div class="at-val" style="font-size:12px;color:#a0aec0">${a.open_time||'—'}</div></div>
      <div class="at-box" style="border:1px solid ${pc}44">
        <div class="at-lbl">Live P&L</div>
        <div class="at-pnl" id="at-live-pnl" style="color:${pc}">${pnl>=0?'+':''}₹${Math.round(Math.abs(pnl)).toLocaleString('en-IN')} (${pct>=0?'+':''}${pct.toFixed(1)}%)</div>
      </div>
    </div>`;
}

// ── Trade Table ──
function renderTrades(trades){
  const el=document.getElementById('ttable');
  if(!trades||!trades.length){el.innerHTML='<div class="nodata">No trades yet</div>';return;}
  el.innerHTML=`<table>
    <tr><th>Date</th><th>Symbol</th><th>Option</th><th>Strike</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Exit Type</th></tr>
    ${[...trades].reverse().slice(0,50).map(t=>{
      const pc=t.pnl>=0?'#48bb78':'#fc8181';
      const sc=t.status&&t.status.includes('SL')?'br2':t.status&&t.status.includes('TRAIL')?'bg2':'by2';
      return`<tr>
        <td style="color:#718096">${t.date}</td>
        <td><b>${t.symbol}</b></td>
        <td style="color:#a0aec0;font-size:11px">${t.opt_sym||'—'}</td>
        <td>${t.strike||'—'}</td>
        <td><span class="b ${t.direction==='CALL'?'bg2':'br2'}">${t.direction}</span></td>
        <td>${(t.entry||0).toFixed(1)}</td>
        <td>${(t.exit||0).toFixed(1)}</td>
        <td style="font-weight:700;color:${pc}">${fp(t.pnl)}</td>
        <td><span class="b ${sc}">${t.status||'—'}</span></td>
      </tr>`;
    }).join('')}
  </table>`;
}

// ── Socket.IO ──
const socket=io({transports:['websocket','polling']});
socket.on('connect',()=>{
  document.getElementById('ws-dot').className='dot dot-g';
  document.getElementById('ws-txt').textContent='WebSocket Live';
});
socket.on('disconnect',()=>{
  document.getElementById('ws-dot').className='dot dot-r';
  document.getElementById('ws-txt').textContent='Disconnected';
});
socket.on('ws_status',d=>{
  document.getElementById('ws-dot').className='dot '+(d.connected?'dot-g':'dot-r');
  document.getElementById('ws-txt').textContent=d.connected?'Kite WS Live':'Kite WS Down';
});

socket.on('tick', applyTick);
socket.on('scan_update', d=>{applyTick(d);applyScan(d);});
socket.on('active_trade', d=>renderActive(d.active));

socket.on('snapshot', d=>{
  if(d.market){
    Object.entries(d.market).forEach(([key,m])=>{
      if(m&&m.ltp) applyTick({sym:key,...m});
      if(m) applyScan({sym:key,...m});
    });
  }
  if(d.trades) renderTrades(d.trades);
  if(d.active!==undefined) renderActive(d.active);
});

// Poll active trade every 2s — uses cur_ltp from /api/active (option LTP)
setInterval(()=>{
  const panel=document.getElementById('active-panel');
  if(!panel.classList.contains('atrade'))return;
  fetch('/api/active').then(r=>r.json()).then(d=>renderActive(d.active)).catch(()=>{});
},2000);

setInterval(()=>{
  fetch('/api/trades').then(r=>r.json()).then(renderTrades).catch(()=>{});
},30000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
#  START
# ─────────────────────────────────────────────
def run_dashboard():
    socketio.run(app, host="0.0.0.0", port=PORT,
                 debug=False, use_reloader=False)

if __name__ == "__main__":
    run_dashboard()
