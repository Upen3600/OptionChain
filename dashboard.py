"""
╔══════════════════════════════════════════════════════════════════╗
║   FRIDAY DUAL-BETA DASHBOARD — Flask + SocketIO                  ║
║   Tick-by-Tick LTP | Live Trade P&L (Option Price Based)         ║
╚══════════════════════════════════════════════════════════════════╝
"""

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
app.config["SECRET_KEY"] = "friday_dual_beta_2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── Shared state ──
_tick_data = {
    info["key"]: {
        "name": info["name"], "ltp": 0, "high": 0, "low": 0,
        "close": 0, "open": 0, "change": 0, "change_pct": 0,
        "volume": 0, "time": "--:--:--",
        "status": "Waiting...", "last_scan": "--:--:--",
    }
    for info in SYMBOL_TOKENS.values()
}
_active_trades: dict = {}
_ticker      = None
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
    """Called by scanner after each candle close."""
    key = next((v["key"] for k, v in SYMBOL_TOKENS.items()
                if v["name"] == symbol), None)
    if not key:
        return
    _tick_data[key].update({
        "status":    data.get("status", ""),
        "last_scan": data.get("last_scan", "--:--:--"),
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

            # Live P&L update for active trade
            active_list = list(_active_trades.values())
            active = next((t for t in active_list if t.get("sym_key") == key), None)
            if active:
                # Update cur_ltp using scanner's live value
                if _scanner_ref and info["name"] in _scanner_ref.active_trades:
                    st = _scanner_ref.active_trades[info["name"]]
                    active["cur_ltp"] = st.get("cur_ltp", active.get("cur_ltp", 0))
                    active["sl"]      = st.get("sl", active.get("sl", 0))
                    active["target"]  = st.get("target", active.get("target", 0))
                socketio.emit("trade_ltp_update", {
                    "sym_key": key,
                    "cur_ltp": active.get("cur_ltp", 0),
                    "sl":      active.get("sl", 0),
                    "target":  active.get("target", 0),
                })

            # Emit tick
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
        index_tokens = list(SYMBOL_TOKENS.keys())
        ws.subscribe(index_tokens)
        ws.set_mode(ws.MODE_FULL, index_tokens)
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
    active_list = list(_active_trades.values())
    return {"market": _tick_data, "trades": trades,
            "active": active_list[0] if active_list else None}


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
    # Sync cur_ltp from scanner before returning
    for key, trade in _active_trades.items():
        if _scanner_ref:
            sym = trade.get("symbol", "")
            st  = _scanner_ref.active_trades.get(sym)
            if st:
                trade["cur_ltp"] = st.get("cur_ltp", trade.get("cur_ltp", 0))
                trade["sl"]      = st.get("sl", trade.get("sl", 0))
                trade["target"]  = st.get("target", trade.get("target", 0))
    active_list = list(_active_trades.values())
    return {"active": active_list[0] if active_list else None}


@socketio.on("connect")
def on_client_connect():
    trades = []
    if os.path.exists(TRADE_FILE):
        try:
            with open(TRADE_FILE) as f:
                trades = json.load(f)
        except Exception:
            pass
    active_list = list(_active_trades.values())
    socketio.emit("snapshot", {
        "market": _tick_data, "trades": trades,
        "active": active_list[0] if active_list else None
    })


# ─────────────────────────────────────────────
#  HTML DASHBOARD
# ─────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Friday Dual-Beta — Live Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#080b14;color:#e2e8f0;min-height:100vh}

/* Header */
header{background:#0e1120;border-bottom:1px solid #1e2535;padding:12px 20px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.logo{font-size:14px;font-weight:700;color:#fff;letter-spacing:.5px}
.logo span{color:#f6c90e}
.pills{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{display:flex;align-items:center;gap:5px;background:#151829;
  border:1px solid #2d3748;border-radius:20px;padding:3px 10px;font-size:11px;color:#a0aec0}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot-g{background:#48bb78;box-shadow:0 0 6px #48bb7880;animation:pulse 1.5s infinite}
.dot-r{background:#fc8181}
.dot-y{background:#f6c90e;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.clock-box{text-align:right;flex-shrink:0}
.clock-t{font-size:22px;font-weight:900;color:#fff;letter-spacing:2px;font-variant-numeric:tabular-nums}
.clock-d{font-size:10px;color:#4a5568;margin-top:1px}

/* ─── HERO: NF + BN big LTP ─── */
.hero{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:18px 14px 0;max-width:1280px;margin:0 auto}
@media(max-width:600px){.hero{grid-template-columns:1fr}}
.hero-card{background:#0e1120;border-radius:18px;padding:20px 24px;position:relative;overflow:hidden}
.hero-card.nf{border:1.5px solid #f5576c55;box-shadow:0 0 30px #f5576c18}
.hero-card.bn{border:1.5px solid #667eea55;box-shadow:0 0 30px #667eea18}
.hero-bg{position:absolute;right:-10px;top:-10px;font-size:80px;opacity:.04;font-weight:900;pointer-events:none;user-select:none}
.hero-name{font-size:11px;font-weight:700;letter-spacing:1.5px;color:#718096;margin-bottom:6px}
.hero-ltp{font-size:44px;font-weight:900;letter-spacing:-1px;font-variant-numeric:tabular-nums;
  transition:color .15s;line-height:1}

.hero-card.nf .hero-ltp{color:#f87171}
.hero-card.bn .hero-ltp{color:#818cf8}
.hero-meta{display:flex;gap:12px;align-items:center;margin-top:8px;flex-wrap:wrap}
.hero-chg{font-size:13px;font-weight:700;padding:3px 9px;border-radius:7px}
.cup{background:#1a3a2a;color:#48bb78}.cdn{background:#3a1a1a;color:#fc8181}.cfl{background:#1e2535;color:#718096}
.hero-hl{font-size:11px;color:#4a5568;display:flex;gap:10px}
.hero-hl span{color:#a0aec0;font-weight:600}
.hero-time{font-size:10px;color:#2d3748;margin-left:auto}
.hero-status{font-size:11px;padding:2px 8px;border-radius:6px;font-weight:700;margin-left:auto}
.hs-scan{background:#2a2a1a;color:#f6c90e}
.hs-ce{background:#1a3a2a;color:#48bb78}
.hs-pe{background:#3a1a1a;color:#fc8181}
.hs-wait{background:#151829;color:#4a5568}

.container{max-width:1280px;margin:0 auto;padding:14px}
.section-label{font-size:10px;color:#4a5568;text-transform:uppercase;letter-spacing:1.2px;
  margin-bottom:10px;margin-top:6px;padding-left:2px}

/* ─── Small Index Cards (FN, SX) ─── */
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:22px}
@media(max-width:520px){.mgrid{grid-template-columns:1fr}}
.mcard{background:#0e1120;border:1px solid #1e2535;border-radius:14px;padding:14px 16px}
.mcard.fn{border-top:3px solid #f6c90e}
.mcard.sx{border-top:3px solid #48bb78}
.mc-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.mc-name{font-size:10px;font-weight:700;color:#a0aec0;letter-spacing:1px}
.mc-badge{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:700}
.bo{background:#1a3a2a;color:#48bb78}.bc{background:#2a1a1a;color:#fc8181}.bp{background:#2a2a1a;color:#f6c90e}
.ltp{font-size:22px;font-weight:900;color:#fff;font-variant-numeric:tabular-nums;display:block;margin-bottom:2px}
.chg-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.chg{font-size:11px;font-weight:700;padding:2px 6px;border-radius:5px}
.tick-time{font-size:10px;color:#4a5568}
.hl-row{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.hl-box{background:#080b14;border-radius:6px;padding:5px 7px}
.hl-lbl{font-size:9px;color:#4a5568;text-transform:uppercase;letter-spacing:.4px;margin-bottom:1px}
.hl-v{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums}
.hv-h{color:#48bb78}.hv-l{color:#fc8181}

/* ─── Active Trade ─── */
.atrade-wrap{margin-bottom:22px}
.atrade{background:#0e1120;border:2px solid #f6c90e66;border-radius:16px;padding:18px 20px}
.atrade-none{background:#0e1120;border:1px solid #1e2535;border-radius:14px;padding:16px;
  text-align:center;color:#2d3748;font-size:13px}
.at-title{font-size:11px;color:#f6c90e;text-transform:uppercase;letter-spacing:1.2px;
  margin-bottom:12px;display:flex;align-items:center;gap:8px}
.at-sym{background:#1a1d2e;padding:2px 8px;border-radius:6px;color:#a0aec0;font-size:10px}
.at-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.at-box{background:#080b14;border-radius:9px;padding:9px 11px}
.at-lbl{font-size:9px;color:#4a5568;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.at-val{font-size:14px;font-weight:800;font-variant-numeric:tabular-nums}
.at-pnl-box{background:#080b14;border-radius:9px;padding:9px 11px;grid-column:span 2}
@media(max-width:500px){.at-pnl-box{grid-column:span 1}}
.at-pnl{font-size:28px;font-weight:900;font-variant-numeric:tabular-nums;line-height:1.1}
.at-pnl-sub{font-size:11px;color:#718096;margin-top:3px}

/* ─── Trade Table ─── */
.panel{background:#0e1120;border:1px solid #1e2535;border-radius:12px;padding:14px;margin-bottom:18px}
.twrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:560px}
th{text-align:left;padding:7px 9px;color:#4a5568;font-weight:500;
  border-bottom:1px solid #1e2535;font-size:9px;text-transform:uppercase;letter-spacing:.6px}
td{padding:8px 9px;border-bottom:1px solid #080b14;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}tr:hover td{background:#151829}
.b{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10px;font-weight:700}
.bg2{background:#1a3a2a;color:#48bb78}.br2{background:#3a1a1a;color:#fc8181}
.by2{background:#3a3010;color:#f6c90e}.bb2{background:#1a2a3a;color:#63b3ed}
.nodata{text-align:center;color:#2d3748;padding:28px;font-size:13px}
.footer{text-align:center;font-size:10px;color:#2d3748;padding:12px 0 24px}
</style>
</head>
<body>

<header>
  <div class="pills">
    <span class="logo">⚡ Friday <span>Dual-Beta</span></span>
    <div class="pill"><span class="dot dot-y" id="ws-dot"></span><span id="ws-txt">Connecting...</span></div>
    <div class="pill"><span class="dot dot-g"></span><span>Option Price SL/Target</span></div>
  </div>
  <div class="clock-box">
    <div class="clock-t" id="clock">--:--:--</div>
    <div class="clock-d" id="cdate">Asia / Kolkata</div>
  </div>
</header>

<!-- ─── HERO: NIFTY + BANKNIFTY Big LTP ─── -->
<div class="hero">
  <div class="hero-card nf">
    <div class="hero-bg">NF</div>
    <div class="hero-name">NIFTY 50 &nbsp;<span class="mc-badge bp" id="nf-badge">—</span></div>
    <div class="hero-ltp" id="nf-ltp">—</div>
    <div class="hero-meta">
      <span class="hero-chg cfl" id="nf-chg">—</span>
      <div class="hero-hl">
        H:<span id="nf-high">—</span>&nbsp;&nbsp;L:<span id="nf-low">—</span>
      </div>
      <span class="hero-time" id="nf-time">—</span>
    </div>
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      <span style="font-size:10px;color:#4a5568">Scanner:</span>
      <span class="hero-status hs-wait" id="nf-status">Loading...</span>
      <span style="font-size:10px;color:#2d3748" id="nf-scan-ts"></span>
    </div>
  </div>

  <div class="hero-card bn">
    <div class="hero-bg">BN</div>
    <div class="hero-name">BANK NIFTY &nbsp;<span class="mc-badge bp" id="bn-badge">—</span></div>
    <div class="hero-ltp" id="bn-ltp">—</div>
    <div class="hero-meta">
      <span class="hero-chg cfl" id="bn-chg">—</span>
      <div class="hero-hl">
        H:<span id="bn-high">—</span>&nbsp;&nbsp;L:<span id="bn-low">—</span>
      </div>
      <span class="hero-time" id="bn-time">—</span>
    </div>
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      <span style="font-size:10px;color:#4a5568">Scanner:</span>
      <span class="hero-status hs-wait" id="bn-status">Loading...</span>
      <span style="font-size:10px;color:#2d3748" id="bn-scan-ts"></span>
    </div>
  </div>
</div>

<div class="container">

  <!-- FN + SX smaller cards -->
  <div class="section-label" style="margin-top:18px">Other Indices</div>
  <div class="mgrid">
    <div class="mcard fn">
      <div class="mc-head"><span class="mc-name">FIN NIFTY</span><span class="mc-badge bp" id="fn-badge">—</span></div>
      <span class="ltp" id="fn-ltp">—</span>
      <div class="chg-row"><span class="chg cfl" id="fn-chg">—</span><span class="tick-time" id="fn-time">—</span></div>
      <div class="hl-row">
        <div class="hl-box"><div class="hl-lbl">High</div><div class="hl-v hv-h" id="fn-high">—</div></div>
        <div class="hl-box"><div class="hl-lbl">Low</div><div class="hl-v hv-l" id="fn-low">—</div></div>
      </div>
    </div>
    <div class="mcard sx">
      <div class="mc-head"><span class="mc-name">SENSEX</span><span class="mc-badge bp" id="sx-badge">—</span></div>
      <span class="ltp" id="sx-ltp">—</span>
      <div class="chg-row"><span class="chg cfl" id="sx-chg">—</span><span class="tick-time" id="sx-time">—</span></div>
      <div class="hl-row">
        <div class="hl-box"><div class="hl-lbl">High</div><div class="hl-v hv-h" id="sx-high">—</div></div>
        <div class="hl-box"><div class="hl-lbl">Low</div><div class="hl-v hv-l" id="sx-low">—</div></div>
      </div>
    </div>
  </div>

  <!-- Active Trade -->
  <div class="section-label">Active Position</div>
  <div class="atrade-wrap">
    <div id="active-panel" class="atrade-none">⏳ No open position</div>
  </div>

  <!-- Trade History -->
  <div class="section-label">Trade History</div>
  <div class="panel">
    <div class="twrap" id="ttable"><div class="nodata">No trades yet</div></div>
  </div>

  <div class="footer">Friday Dual-Beta · Option-Price Based SL & Targets · Zerodha Kite</div>
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

// ── Number Formatters ──
function fn(n,d=2){return n==null||isNaN(n)?'—':Number(n).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:d});}
function fp(n){if(n==null||isNaN(n))return'—';const s=n<0?'':'+';;return s+'₹'+Math.abs(n).toLocaleString('en-IN',{maximumFractionDigits:0});}

const prevLtp={nf:0,bn:0,fn:0,sx:0};

// ── Tick Update ──
function applyTick(d){
  const s=d.sym;
  const isHero=(s==='nf'||s==='bn');
  const el=document.getElementById(s+'-ltp');
  if(!el)return;
  const prev=prevLtp[s],ltp=d.ltp;
  prevLtp[s]=ltp;
  el.textContent=fn(ltp);

  // High / Low
  const h=document.getElementById(s+'-high');
  const l=document.getElementById(s+'-low');
  const t=document.getElementById(s+'-time');
  if(h)h.textContent=fn(d.high);
  if(l)l.textContent=fn(d.low);
  if(t)t.textContent=d.time||'';

  // Change
  const chg=d.change||0,chgp=d.change_pct||0;
  const cEl=document.getElementById(s+'-chg');
  if(cEl){
    cEl.textContent=(chg>=0?'+':'')+fn(chg)+' ('+(chgp>=0?'+':'')+fn(chgp)+'%)';
    cEl.className=(isHero?'hero-chg ':'chg ')+(chg>0?'cup':chg<0?'cdn':'cfl');
  }
}

// ── Status Update ──
function applyStatus(s,status,last_scan){
  const sts=document.getElementById(s+'-status');
  const ts=document.getElementById(s+'-scan-ts');
  if(sts){
    sts.textContent=status||'—';
    sts.className='hero-status '+
      (status&&status.includes('TRADE CE')?'hs-ce':
       status&&status.includes('TRADE PE')?'hs-pe':
       status&&(status.includes('Scan')||status.includes('No Trade'))?'hs-scan':'hs-wait');
  }
  if(ts)ts.textContent=last_scan?'| '+last_scan:'';
}

// ── Active Trade Render ──
function renderActive(a){
  const panel=document.getElementById('active-panel');
  if(!a){
    panel.className='atrade-none';
    panel.innerHTML='⏳ No open position';
    return;
  }
  const optLtp  = a.cur_ltp||a.entry||0;
  const entry   = a.entry||0;
  const lotQty  = a.lot_qty||1;
  const pnl     = (optLtp-entry)*lotQty;
  const pct     = entry?(optLtp-entry)/entry*100:0;
  const pc      = pnl>=0?'#48bb78':'#fc8181';
  const dc      = a.direction==='CALL'?'#48bb78':'#fc8181';
  const sl      = a.sl||0;
  const tgt     = a.target||0;
  const pattern = a.pattern||'';

  panel.className='atrade';
  panel.innerHTML=`
    <div class="at-title">
      <span class="dot ${pnl>=0?'dot-g':'dot-r'}"></span>
      LIVE TRADE — ${a.symbol||''}
      <span class="at-sym">${pattern}</span>
    </div>
    <div class="at-grid">
      <div class="at-box">
        <div class="at-lbl">Option</div>
        <div class="at-val" style="font-size:10px;color:#a0aec0">${a.option_symbol||'—'}</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">Side</div>
        <div class="at-val" style="color:${dc}">${a.side||'—'} (${a.direction||''})</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">Option Entry</div>
        <div class="at-val">₹${entry.toFixed(2)}</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">Live Option LTP</div>
        <div class="at-val" id="live-opt-ltp" style="color:${optLtp>=entry?'#48bb78':'#fc8181'}">₹${optLtp.toFixed(2)}</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">SL (Option)</div>
        <div class="at-val" style="color:#fc8181">₹${sl.toFixed(2)}</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">Target (Option)</div>
        <div class="at-val" style="color:#48bb78">₹${tgt.toFixed(2)}</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">Index Entry</div>
        <div class="at-val" style="color:#a0aec0">₹${(a.entry_index||0).toFixed(1)}</div>
      </div>
      <div class="at-box">
        <div class="at-lbl">Opened</div>
        <div class="at-val" style="font-size:11px;color:#718096">${a.open_time||'—'}</div>
      </div>
      <div class="at-pnl-box" style="border:1px solid ${pc}33">
        <div class="at-lbl">Live P&amp;L (1 lot × ${lotQty})</div>
        <div class="at-pnl" style="color:${pc}">${pnl>=0?'+':''}₹${Math.round(Math.abs(pnl)).toLocaleString('en-IN')} <span style="font-size:16px">(${pct>=0?'+':''}${pct.toFixed(1)}%)</span></div>
        <div class="at-pnl-sub">Entry ₹${entry.toFixed(2)} → LTP ₹${optLtp.toFixed(2)} | ${(optLtp-entry>=0?'+':'')}${(optLtp-entry).toFixed(2)} pts</div>
      </div>
    </div>`;
}

// ── Trade Table ──
function renderTrades(trades){
  const el=document.getElementById('ttable');
  if(!trades||!trades.length){el.innerHTML='<div class="nodata">No trades yet</div>';return;}
  el.innerHTML=`<table>
    <tr><th>Date</th><th>Symbol</th><th>Pattern</th><th>Option</th><th>Side</th><th>Entry ₹</th><th>Exit ₹</th><th>P&L</th><th>Type</th></tr>
    ${[...trades].reverse().slice(0,50).map(t=>{
      const pc=t.pnl>=0?'#48bb78':'#fc8181';
      const sc=t.status&&t.status.includes('SL')?'br2':t.status&&t.status.includes('TRAIL')?'bg2':'by2';
      return`<tr>
        <td style="color:#718096">${t.date}</td>
        <td><b>${t.symbol}</b></td>
        <td style="color:#f6c90e;font-size:11px">${t.pattern||'—'}</td>
        <td style="color:#a0aec0;font-size:10px">${t.opt_sym||'—'}</td>
        <td><span class="b ${t.direction==='CALL'?'bg2':'br2'}">${t.direction}</span></td>
        <td>${(t.entry||0).toFixed(2)}</td>
        <td>${(t.exit||0).toFixed(2)}</td>
        <td style="font-weight:800;color:${pc}">${fp(t.pnl)}</td>
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

socket.on('tick',applyTick);

socket.on('scan_update',d=>{
  applyTick(d);
  applyStatus(d.sym,d.status,d.last_scan);
});

socket.on('active_trade',d=>renderActive(d.active));

// Live option LTP update on every tick
socket.on('trade_ltp_update',d=>{
  const panel=document.getElementById('active-panel');
  if(!panel.classList.contains('atrade'))return;
  // Update live LTP in P&L box without full re-render
  const cur=d.cur_ltp||0;
  const el=document.getElementById('live-opt-ltp');
  if(el)el.textContent='₹'+cur.toFixed(2);
  // Full re-render from /api/active every 2s handles P&L
});

socket.on('snapshot',d=>{
  if(d.market){
    Object.entries(d.market).forEach(([key,m])=>{
      if(m&&m.ltp)applyTick({sym:key,...m});
      if(m)applyStatus(key,m.status,m.last_scan);
    });
  }
  if(d.trades)renderTrades(d.trades);
  if(d.active!==undefined)renderActive(d.active);
});

// Poll active trade P&L every 2 seconds for live option price
setInterval(()=>{
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
    import eventlet
    import eventlet.wsgi
    eventlet.monkey_patch()
    socketio.run(app, host="0.0.0.0", port=PORT,
                 debug=False, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
