"""
Live Paper Trading Dashboard — HTTP Server
=============================================
Serves a Bloomberg Terminal-style dashboard with TradingView-inspired
candlestick chart.  READ-ONLY: reads log files, never modifies state.

Usage:
    python scripts/live_dashboard.py              # Standalone on port 8080
    python scripts/live_dashboard.py --port 9090  # Custom port

API endpoints:
    GET /              -> Dashboard HTML
    GET /api/status    -> logs/paper_trading_state.json
    GET /api/decisions -> logs/trade_decisions.json (last 50)
    GET /api/candles   -> logs/candle_buffer.json
    GET /api/trades    -> logs/active_trades.json
    GET /api/modifiers -> logs/modifier_state.json
    GET /api/safety    -> logs/safety_state.json
"""

import argparse
import json
import logging
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
LOGS_DIR = PROJECT_DIR / "logs"

# ── Default empty responses for missing files ──
DEFAULTS = {
    "status": {
        "trade_count": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
        "sharpe_estimate": 0.0, "max_drawdown": 0.0, "current_drawdown": 0.0,
    },
    "decisions": [],
    "candles": [],
    "trades": [],
    "modifiers": {
        "overnight": {"value": 1.0, "reason": "No data"},
        "fomc": {"value": 1.0, "reason": "No data"},
        "gamma": {"value": 1.0, "reason": "No data"},
        "har_rv": {"value": 1.0, "reason": "No data"},
        "total": 1.0,
    },
    "safety": {
        "daily_pnl": 0.0, "daily_limit": 500.0,
        "consec_losses": 0, "max_consec": 5,
        "position_size": 0, "max_position": 2,
        "heartbeat_age_sec": 0.0, "all_ok": True,
    },
}

# File mapping: endpoint -> (filename, default_key, is_jsonl)
FILE_MAP = {
    "/api/status":    ("paper_trading_state.json", "status",    False),
    "/api/decisions": ("trade_decisions.json",     "decisions", True),
    "/api/candles":   ("candle_buffer.json",       "candles",   False),
    "/api/trades":    ("active_trades.json",       "trades",    False),
    "/api/modifiers": ("modifier_state.json",      "modifiers", False),
    "/api/safety":    ("safety_state.json",        "safety",    False),
}


def _read_json_file(filepath: Path, default_key: str, is_jsonl: bool = False, limit: int = 50):
    """Read a JSON or JSONL file, returning default on missing/error."""
    try:
        if not filepath.exists():
            return DEFAULTS[default_key]

        text = filepath.read_text(encoding="utf-8").strip()
        if not text:
            return DEFAULTS[default_key]

        if is_jsonl:
            lines = text.split("\n")
            result = []
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return result
        else:
            return json.loads(text)

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Error reading %s: %s", filepath, e)
        return DEFAULTS[default_key]


def atomic_write_json(filepath: Path, data) -> None:
    """Write JSON atomically: write to .tmp then rename."""
    tmp_path = filepath.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        os.replace(str(tmp_path), str(filepath))
    except OSError as e:
        logger.warning("Atomic write failed for %s: %s", filepath, e)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NQ.BOT — Live Trading Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg-primary:#0a0e14;
  --bg-secondary:#0d1117;
  --bg-panel:#0b1018;
  --border:#1a2332;
  --grid:#141c28;
  --text-primary:#e2e8f0;
  --text-secondary:#6b7a8d;
  --text-muted:#3d4a5c;
  --green:#00d4aa;
  --green-fill:rgba(0,212,170,0.12);
  --red:#ff3b5c;
  --red-fill:rgba(255,59,92,0.12);
  --amber:#ffb800;
  --amber-fill:rgba(255,184,0,0.12);
  --blue:#4da6ff;
  --blue-fill:rgba(77,166,255,0.12);
}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg-primary);
  color:var(--text-primary);
  font-family:'JetBrains Mono','Fira Code','SF Mono','Cascadia Code',monospace;
  font-size:12px;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  min-width:1024px;
}

/* ── HEADER BAR ── */
.header{
  display:flex;align-items:center;padding:0 16px;
  background:var(--bg-panel);border-bottom:1px solid var(--border);
  height:40px;gap:12px;
}
.logo{font-size:16px;font-weight:700;letter-spacing:0.5px;white-space:nowrap}
.logo-nq{color:var(--green)}.logo-bot{color:#4a5568}
.badge-paper{
  padding:1px 6px;border-radius:3px;font-size:9px;font-weight:600;
  letter-spacing:1px;text-transform:uppercase;white-space:nowrap;
  background:rgba(255,184,0,0.15);color:var(--amber);border:1px solid rgba(255,184,0,0.3);
}
.hdr-sym{color:var(--text-secondary);font-size:12px;white-space:nowrap}
.hdr-price{color:var(--text-primary);font-size:18px;font-weight:700;white-space:nowrap}
.hdr-change{font-size:12px;white-space:nowrap}
.hdr-change.up{color:var(--green)}.hdr-change.down{color:var(--red)}
.hdr-sep{width:1px;height:20px;background:var(--border);flex-shrink:0}
.tf-buttons{display:flex;gap:4px;align-items:center}
.tf-btn{
  background:transparent;color:var(--text-secondary);border:1px solid var(--border);
  border-radius:3px;padding:2px 8px;height:22px;min-width:36px;
  font-family:inherit;font-size:10px;cursor:pointer;transition:all .12s;
  display:flex;align-items:center;justify-content:center;
}
.tf-btn:hover{background:var(--border);color:var(--text-primary)}
.tf-btn.active{background:rgba(0,212,170,0.15);color:var(--green);border-color:var(--green)}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.live-dot{
  width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0;
  animation:pulse 2s infinite;
}
.live-dot.disconnected{background:var(--red);animation:none}
@keyframes pulse{
  0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,212,170,0.4)}
  50%{opacity:.7;box-shadow:0 0 0 6px rgba(0,212,170,0)}
}
.live-label{font-size:10px;font-weight:600;letter-spacing:1px;white-space:nowrap}
.live-label.on{color:var(--green)}.live-label.off{color:var(--red)}
.clock{color:var(--text-secondary);font-size:12px;white-space:nowrap}

/* ── STATS BAR ── */
.stats-bar{
  display:flex;height:48px;
  background:var(--bg-panel);border-bottom:1px solid var(--border);
  padding:6px 16px;gap:2px;
}
.stat-card{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
}
.stat-lbl{font-size:8px;text-transform:uppercase;color:#4a5568;letter-spacing:1.5px;line-height:1}
.stat-val{font-size:20px;font-weight:700;line-height:1.2}
.stat-sub{font-size:8px;color:var(--text-muted)}
.stat-val.pos{color:var(--green)}.stat-val.neg{color:var(--red)}.stat-val.neut{color:var(--text-primary)}
.stat-val.warn{color:var(--amber)}

/* ── MAIN LAYOUT ── */
.main{display:flex;height:calc(100vh - 40px - 48px - 200px)}
.chart-col{flex:1;display:flex;flex-direction:column;position:relative;background:var(--bg-secondary);overflow:hidden}
.chart-wrap{flex:1;position:relative;overflow:hidden}
#chartCanvas{display:block;width:100%;height:100%}
.ohlc-bar{
  position:absolute;top:8px;left:12px;z-index:10;pointer-events:none;
  font-size:11px;line-height:1.4;
  background:rgba(13,17,23,0.8);padding:4px 8px;border-radius:3px;
}
.ohlc-title{color:var(--text-secondary);font-weight:700;font-size:11px}
.ohlc-tf{color:#4a5568;font-size:11px}
.ohlc-dot{color:var(--green);font-size:10px}
.ohlc-lbl{color:#4a5568}.ohlc-up{color:var(--green)}.ohlc-dn{color:var(--red)}
.ohlc-vol{color:#4a5568;font-size:10px}

/* ── SIDEBAR ── */
.sidebar{
  width:240px;background:var(--bg-panel);border-left:1px solid var(--border);
  overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:8px 0;
}
@media(max-width:1279px){.sidebar{display:none}.chart-col{border-right:none}}
.side-hdr{
  font-size:10px;text-transform:uppercase;color:#4a5568;letter-spacing:1.5px;
  padding:0 12px 6px;border-bottom:1px solid var(--border);margin-bottom:4px;
}
.side-section{padding:0 12px}
.pos-card{
  padding:8px;border-radius:4px;margin-bottom:6px;
}
.pos-card.long-card{background:rgba(0,212,170,0.08);border:1px solid rgba(0,212,170,0.25)}
.pos-card.short-card{background:rgba(255,59,92,0.08);border:1px solid rgba(255,59,92,0.25)}
.pos-dir-badge{font-size:10px;font-weight:700}
.pos-dir-badge.long{color:var(--green)}.pos-dir-badge.short{color:var(--red)}
.pos-entry{color:var(--text-primary);font-size:11px;margin-top:2px}
.pos-pnl{font-size:14px;font-weight:700;margin-top:3px}
.pos-timer{color:var(--amber);font-size:10px;font-weight:700;margin-top:2px}
.pos-mod{color:#4a5568;font-size:8px;margin-top:1px}
.pos-empty{color:var(--text-muted);font-size:10px;padding:8px 0}

/* Safety Rails */
.rail{margin-bottom:8px}
.rail-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}
.rail-name{font-size:9px;color:var(--text-secondary)}
.rail-status{font-size:9px;font-weight:700}
.rail-status.ok{color:var(--green)}.rail-status.warn{color:var(--amber)}.rail-status.alert{color:var(--red)}
.rail-bar{height:3px;background:var(--grid);border-radius:2px;overflow:hidden}
.rail-fill{height:100%;border-radius:2px;transition:width .4s ease}
.rail-val{font-size:8px;color:var(--text-muted);margin-top:2px}

/* Modifiers */
.mod-row{
  display:flex;justify-content:space-between;align-items:flex-start;
  padding:5px 0;border-bottom:1px solid var(--border);
}
.mod-row:last-child{border-bottom:none}
.mod-name{font-size:9px;color:var(--text-secondary)}
.mod-right{text-align:right}
.mod-val{font-size:10px;font-weight:700}
.mod-val.hi{color:var(--green)}.mod-val.lo{color:var(--amber)}.mod-val.dim{color:var(--text-secondary)}
.mod-reason{font-size:7px;color:var(--text-muted);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mod-total-row{
  display:flex;justify-content:space-between;align-items:center;
  padding-top:6px;margin-top:4px;border-top:1px solid var(--border);
}
.mod-total-lbl{font-size:10px;font-weight:700;color:var(--text-primary)}
.mod-total-val{font-size:14px;font-weight:700;color:var(--blue)}

/* ── DECISIONS TABLE ── */
.decisions{
  height:200px;background:var(--bg-panel);border-top:1px solid var(--border);
  display:flex;flex-direction:column;
}
.dec-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:6px 16px;border-bottom:1px solid var(--border);flex-shrink:0;
}
.dec-title{font-size:10px;font-weight:700;color:#4a5568;text-transform:uppercase;letter-spacing:1.2px}
.dec-count{font-size:9px;color:var(--text-muted)}
.dec-scroll{flex:1;overflow-y:auto}
.dec-scroll::-webkit-scrollbar{width:6px}
.dec-scroll::-webkit-scrollbar-track{background:var(--border)}
.dec-scroll::-webkit-scrollbar-thumb{background:var(--text-muted);border-radius:3px}
table{width:100%;border-collapse:collapse}
thead th{
  position:sticky;top:0;background:var(--bg-panel);z-index:1;
  font-size:8px;text-transform:uppercase;color:var(--text-muted);letter-spacing:1.2px;
  padding:5px 10px;text-align:left;border-bottom:1px solid var(--border);
}
tbody td{padding:4px 10px;font-size:10px;height:28px;border-bottom:1px solid rgba(26,35,50,0.5)}
tbody tr:nth-child(even){background:rgba(13,17,23,0.3)}
tbody tr:hover{background:rgba(77,166,255,0.04)}
.td-time{color:var(--text-secondary);font-size:10px}
.td-dir{font-size:10px;font-weight:700}
.td-dir.long{color:var(--green)}.td-dir.short{color:var(--red)}
.td-price{color:var(--text-primary);font-size:10px}
.badge-dec{padding:1px 6px;border-radius:2px;font-size:8px;font-weight:700;display:inline-block}
.badge-dec.approved{background:rgba(0,212,170,0.15);color:var(--green)}
.badge-dec.rejected{background:rgba(255,59,92,0.15);color:var(--red)}
.td-score{color:var(--text-secondary);font-size:10px}
.td-mod{color:var(--text-secondary);font-size:10px}
.td-reason{color:var(--text-secondary);font-size:10px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.no-data{color:var(--text-muted);text-align:center;padding:30px;font-size:10px}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="logo"><span class="logo-nq">NQ</span><span class="logo-bot">.BOT</span></div>
  <span class="badge-paper">PAPER</span>
  <span class="hdr-sym">MNQ</span>
  <span class="hdr-price" id="hdrPrice">--</span>
  <span class="hdr-change" id="hdrChange">--</span>
  <div class="hdr-sep"></div>
  <div class="tf-buttons" id="tfButtons">
    <button class="tf-btn" data-tf="1m">1m</button>
    <button class="tf-btn active" data-tf="2m">2m</button>
    <button class="tf-btn" data-tf="5m">5m</button>
    <button class="tf-btn" data-tf="15m">15m</button>
    <button class="tf-btn" data-tf="30m">30m</button>
    <button class="tf-btn" data-tf="1H">1H</button>
    <button class="tf-btn" data-tf="4H">4H</button>
    <button class="tf-btn" data-tf="1D">1D</button>
  </div>
  <div class="hdr-right">
    <div style="display:flex;align-items:center;gap:4px">
      <div class="live-dot" id="liveDot"></div>
      <span class="live-label on" id="liveLabel">LIVE</span>
    </div>
    <span class="clock" id="clock">--:--:-- ET</span>
  </div>
</div>

<!-- STATS BAR -->
<div class="stats-bar">
  <div class="stat-card"><div class="stat-lbl">PNL</div><div class="stat-val neut" id="sPnl">$0.00</div></div>
  <div class="stat-card"><div class="stat-lbl">TRADES W/L</div><div class="stat-val neut" id="sTrades">0 (0/0)</div></div>
  <div class="stat-card"><div class="stat-lbl">WIN RATE</div><div class="stat-val neut" id="sWR">0.0%</div></div>
  <div class="stat-card"><div class="stat-lbl">PROFIT FACTOR</div><div class="stat-val neut" id="sPF">0.00</div></div>
  <div class="stat-card"><div class="stat-lbl">SHARPE</div><div class="stat-val neut" id="sSharpe">0.00</div></div>
  <div class="stat-card"><div class="stat-lbl">MAX DD</div><div class="stat-val neut" id="sDD">$0.00</div></div>
  <div class="stat-card"><div class="stat-lbl">BARS</div><div class="stat-val neut" id="sBars">0</div></div>
</div>

<!-- MAIN -->
<div class="main">
  <div class="chart-col">
    <div class="chart-wrap">
      <div class="ohlc-bar" id="ohlcBar">
        <span class="ohlc-title">Micro E-mini Nasdaq-100 Futures</span>
        <span class="ohlc-tf" id="ohlcTF"> · 2 · CME</span>
        &nbsp;&nbsp;
        <span class="ohlc-dot" id="ohlcDot">&#9679;</span>
        <span id="ohlcVals">
          <span class="ohlc-lbl">O</span><span class="ohlc-up" id="oO">--</span>
          <span class="ohlc-lbl"> H</span><span class="ohlc-up" id="oH">--</span>
          <span class="ohlc-lbl"> L</span><span class="ohlc-dn" id="oL">--</span>
          <span class="ohlc-lbl"> C</span><span class="ohlc-up" id="oC">--</span>
          <span id="oChg" class="ohlc-up"> --</span>
        </span>
        <span class="ohlc-vol" id="oVol"></span>
      </div>
      <canvas id="chartCanvas"></canvas>
    </div>
  </div>
  <div class="sidebar">
    <!-- POSITIONS -->
    <div class="side-hdr">ACTIVE POSITIONS</div>
    <div class="side-section" id="posBox"><div class="pos-empty">No open positions</div></div>
    <!-- SAFETY -->
    <div class="side-hdr">SAFETY RAILS</div>
    <div class="side-section" id="safetyBox">
      <div class="rail"><div class="rail-top"><span class="rail-name">Daily Loss</span><span class="rail-status ok" id="rDailySt">OK</span></div><div class="rail-bar"><div class="rail-fill" id="rDailyFill" style="width:0%;background:var(--green)"></div></div><div class="rail-val" id="rDailyVal">$0 / $500</div></div>
      <div class="rail"><div class="rail-top"><span class="rail-name">Consec Losses</span><span class="rail-status ok" id="rConsecSt">OK</span></div><div class="rail-bar"><div class="rail-fill" id="rConsecFill" style="width:0%;background:var(--green)"></div></div><div class="rail-val" id="rConsecVal">0 / 5</div></div>
      <div class="rail"><div class="rail-top"><span class="rail-name">Position Size</span><span class="rail-status ok" id="rPosSt">OK</span></div><div class="rail-bar"><div class="rail-fill" id="rPosFill" style="width:0%;background:var(--green)"></div></div><div class="rail-val" id="rPosVal">0 / 2</div></div>
      <div class="rail"><div class="rail-top"><span class="rail-name">Heartbeat</span><span class="rail-status ok" id="rHBSt">OK</span></div><div class="rail-bar"><div class="rail-fill" id="rHBFill" style="width:0%;background:var(--green)"></div></div><div class="rail-val" id="rHBVal">0s</div></div>
    </div>
    <!-- MODIFIERS -->
    <div class="side-hdr">MODIFIERS</div>
    <div class="side-section" id="modBox">
      <div class="mod-row"><span class="mod-name">OVERNIGHT</span><div class="mod-right"><div class="mod-val dim" id="mON">1.00x</div><div class="mod-reason" id="mONr">No data</div></div></div>
      <div class="mod-row"><span class="mod-name">FOMC</span><div class="mod-right"><div class="mod-val dim" id="mFOMC">1.00x</div><div class="mod-reason" id="mFOMCr">No data</div></div></div>
      <div class="mod-row"><span class="mod-name">GAMMA</span><div class="mod-right"><div class="mod-val dim" id="mGamma">1.00x</div><div class="mod-reason" id="mGammar">No data</div></div></div>
      <div class="mod-row"><span class="mod-name">HAR-RV</span><div class="mod-right"><div class="mod-val dim" id="mHARRV">1.00x</div><div class="mod-reason" id="mHARRVr">No data</div></div></div>
      <div class="mod-total-row"><span class="mod-total-lbl">TOTAL</span><span class="mod-total-val" id="mTotal">1.00x</span></div>
    </div>
  </div>
</div>

<!-- DECISIONS TABLE -->
<div class="decisions">
  <div class="dec-hdr">
    <span class="dec-title">TRADE DECISIONS</span>
    <span class="dec-count" id="decCount">0 approved / 0 rejected</span>
  </div>
  <div class="dec-scroll">
    <table>
      <thead><tr>
        <th>TIME</th><th>DIR</th><th>PRICE</th><th>DECISION</th><th>SCORE</th><th>MODIFIER</th><th>REASON</th>
      </tr></thead>
      <tbody id="decBody"><tr><td colspan="7" class="no-data">No decisions yet</td></tr></tbody>
    </table>
  </div>
</div>

<script>
'use strict';

// ═══════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════
let candles = [], activeTrades = [], decisions = [];
let status = {}, safety = {}, modifiers = {};
let selectedTF = '2m';
let historicalData = {};
let hoverIdx = -1;
let mouseX = -1, mouseY = -1;
let failCount = 0;
let lastCandles = null, lastStatus = null;
let rafId = 0;
let offscreen = null, offCtx = null;

const FONT = "'JetBrains Mono','Fira Code','SF Mono',monospace";
const PAD_TOP = 32, PAD_RIGHT = 70, PAD_BOTTOM = 24, PAD_LEFT = 8;
const VOL_RATIO = 0.12;
const MAX_CANDLES = 100;
const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const MONS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// ═══════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════
const $ = id => document.getElementById(id);
const fmt = (n, d=2) => (n||0).toFixed(d);
const fmtPnl = n => { const v=n||0; return (v>=0?'+':'')+v.toFixed(2); };
const fmtComma = n => n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const pad2 = n => String(n).padStart(2,'0');

function niceInterval(range, maxTicks) {
  const rough = range / maxTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const res = rough / mag;
  let nice;
  if (res <= 1.5) nice = 1;
  else if (res <= 3) nice = 2;
  else if (res <= 7) nice = 5;
  else nice = 10;
  return nice * mag;
}

function toET(d) {
  return new Date(d.toLocaleString('en-US',{timeZone:'America/New_York'}));
}

function isRTH(d) {
  const et = toET(d);
  const h = et.getHours(), m = et.getMinutes();
  const mins = h * 60 + m;
  return mins >= 570 && mins < 960; // 9:30 - 16:00
}

// ═══════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════
function updateClock() {
  const now = new Date();
  const et = now.toLocaleString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  $('clock').textContent = et + ' ET';
}
setInterval(updateClock, 1000);
updateClock();

// ═══════════════════════════════════════════════════
// TIMEFRAME SELECTOR
// ═══════════════════════════════════════════════════
const tfMap = {'1m':'1','2m':'2','5m':'5','15m':'15','30m':'30','1H':'60','4H':'240','1D':'D'};
document.querySelectorAll('.tf-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    selectedTF = btn.dataset.tf;
    $('ohlcTF').textContent = ' \u00b7 ' + (tfMap[selectedTF]||selectedTF) + ' \u00b7 CME';
    fetchHistorical(selectedTF);
  });
});

async function fetchHistorical(tf) {
  try {
    const res = await fetch('/api/historical?timeframe='+encodeURIComponent(tf));
    const data = await res.json();
    if (Array.isArray(data)) { historicalData[tf] = data; scheduleRender(); }
  } catch(e) { console.warn('Historical fetch error:', e); }
}

// ═══════════════════════════════════════════════════
// DATA FETCHING
// ═══════════════════════════════════════════════════
async function fetchData() {
  try {
    const [sR,dR,cR,tR,mR,sfR,hR] = await Promise.all([
      fetch('/api/status').then(r=>r.json()).catch(()=>null),
      fetch('/api/decisions').then(r=>r.json()).catch(()=>null),
      fetch('/api/candles').then(r=>r.json()).catch(()=>null),
      fetch('/api/trades').then(r=>r.json()).catch(()=>null),
      fetch('/api/modifiers').then(r=>r.json()).catch(()=>null),
      fetch('/api/safety').then(r=>r.json()).catch(()=>null),
      fetch('/api/historical?timeframe='+encodeURIComponent(selectedTF)).then(r=>r.json()).catch(()=>null),
    ]);
    failCount = 0;
    $('liveDot').classList.remove('disconnected');
    $('liveLabel').textContent = 'LIVE';
    $('liveLabel').className = 'live-label on';

    if (sR !== null) status = sR;
    if (dR !== null) decisions = dR;
    if (cR !== null) candles = cR;
    if (tR !== null) activeTrades = tR;
    if (mR !== null) modifiers = mR;
    if (sfR !== null) safety = sfR;
    if (Array.isArray(hR)) historicalData[selectedTF] = hR;

    updateUI();
  } catch(e) {
    failCount++;
    if (failCount >= 3) {
      $('liveDot').classList.add('disconnected');
      $('liveLabel').textContent = 'DISCONNECTED';
      $('liveLabel').className = 'live-label off';
    }
    console.warn('Fetch error:', e);
  }
}

// ═══════════════════════════════════════════════════
// UI UPDATES
// ═══════════════════════════════════════════════════
function updateUI() {
  updateHeader();
  updateStats();
  updatePositions();
  updateSafety();
  updateModifiers();
  updateDecisions();
  scheduleRender();
}

function updateHeader() {
  if (!candles.length) return;
  const last = candles[candles.length-1];
  const prev = candles.length > 1 ? candles[candles.length-2] : last;
  const price = last.c;
  const chg = price - prev.c;
  const pct = prev.c ? (chg/prev.c*100) : 0;
  $('hdrPrice').textContent = fmtComma(price);
  const ce = $('hdrChange');
  ce.textContent = fmtPnl(chg) + ' (' + fmtPnl(pct) + '%)';
  ce.className = 'hdr-change ' + (chg >= 0 ? 'up' : 'down');
}

function updateStats() {
  const s = status;
  const pnl = s.total_pnl || 0;
  const pe = $('sPnl');
  pe.textContent = '$' + fmtPnl(pnl);
  pe.className = 'stat-val ' + (pnl >= 0 ? 'pos' : 'neg');

  $('sTrades').textContent = (s.trade_count||0) + ' (' + (s.wins||0) + '/' + (s.losses||0) + ')';

  const wr = s.win_rate || 0;
  const wrE = $('sWR');
  wrE.textContent = fmt(wr,1) + '%';
  wrE.className = 'stat-val ' + (wr > 50 ? 'pos' : wr >= 40 ? 'warn' : wr > 0 ? 'neg' : 'neut');

  const pf = s.profit_factor || 0;
  const pfE = $('sPF');
  pfE.textContent = fmt(pf,2);
  pfE.className = 'stat-val ' + (pf > 1.5 ? 'pos' : pf >= 1.0 ? 'warn' : pf > 0 ? 'neg' : 'neut');

  $('sSharpe').textContent = fmt(s.sharpe_estimate||0,2);

  const dd = s.max_drawdown || 0;
  const ddE = $('sDD');
  ddE.textContent = '$' + fmt(dd,2);
  ddE.className = 'stat-val ' + (dd > 0 ? 'neg' : 'neut');

  $('sBars').textContent = candles.length;
}

// ═══════════════════════════════════════════════════
// POSITIONS
// ═══════════════════════════════════════════════════
function updatePositions() {
  const el = $('posBox');
  if (!Array.isArray(activeTrades) || !activeTrades.length) {
    el.innerHTML = '<div class="pos-empty">No open positions</div>';
    return;
  }
  el.innerHTML = activeTrades.map(t => {
    const isLong = (t.dir||'').toUpperCase() === 'LONG';
    const pnl = t.unrealized_pnl || 0;
    const holdSec = t.entry_time ? Math.floor((Date.now() - new Date(t.entry_time).getTime())/1000) : 0;
    const mm = pad2(Math.floor(holdSec/60));
    const ss = pad2(holdSec % 60);
    return '<div class="pos-card '+(isLong?'long-card':'short-card')+'">'
      +'<div class="pos-dir-badge '+(isLong?'long':'short')+'">'+(t.dir||'?').toUpperCase()+'</div>'
      +'<div class="pos-entry">'+(t.contracts||1)+'x @ '+fmtComma(t.ep||0)+'</div>'
      +'<div class="pos-pnl" style="color:var(--'+(pnl>=0?'green':'red')+')">'+fmtPnl(pnl)+'</div>'
      +'<div class="pos-timer">HOLD '+mm+':'+ss+'</div>'
      +'<div class="pos-mod">Mod: '+fmt(t.modifier||1,2)+'x</div>'
      +'</div>';
  }).join('');
}
setInterval(updatePositions, 1000);

// ═══════════════════════════════════════════════════
// SAFETY RAILS
// ═══════════════════════════════════════════════════
function updateSafety() {
  const s = safety;
  setRail('Daily', Math.abs(s.daily_pnl||0), s.daily_limit||500, '-$'+fmt(Math.abs(s.daily_pnl||0),0)+' / $'+(s.daily_limit||500));
  setRail('Consec', s.consec_losses||0, s.max_consec||5, (s.consec_losses||0)+' / '+(s.max_consec||5));
  setRail('Pos', s.position_size||0, s.max_position||2, (s.position_size||0)+' / '+(s.max_position||2));
  const hb = s.heartbeat_age_sec || 0;
  setRail('HB', Math.min(hb,300), 300, fmt(hb,0)+'s');
}

function setRail(name, val, max, text) {
  const pct = max > 0 ? Math.min(100,(val/max)*100) : 0;
  const fill = $('r'+name+'Fill');
  const st = $('r'+name+'St');
  const vt = $('r'+name+'Val');
  fill.style.width = pct+'%';
  const color = pct < 50 ? 'var(--green)' : pct < 80 ? 'var(--amber)' : 'var(--red)';
  fill.style.background = color;
  st.textContent = pct < 80 ? 'OK' : 'ALERT';
  st.className = 'rail-status ' + (pct < 50 ? 'ok' : pct < 80 ? 'warn' : 'alert');
  vt.textContent = text;
}

// ═══════════════════════════════════════════════════
// MODIFIERS
// ═══════════════════════════════════════════════════
function updateModifiers() {
  const m = modifiers;
  setMod('ON', m.overnight);
  setMod('FOMC', m.fomc);
  setMod('Gamma', m.gamma);
  setMod('HARRV', m.har_rv);
  const te = $('mTotal');
  if (te) te.textContent = fmt(m.total||1.0,2)+'x';
}

function setMod(id, obj) {
  const v = obj && obj.value !== undefined ? obj.value : 1.0;
  const r = obj && obj.reason ? obj.reason : 'No data';
  const ve = $('m'+id);
  const re = $('m'+id+'r');
  if (ve) {
    ve.textContent = fmt(v,2)+'x';
    ve.className = 'mod-val ' + (v > 1.1 ? 'hi' : v < 0.9 ? 'lo' : 'dim');
  }
  if (re) re.textContent = r;
}

// ═══════════════════════════════════════════════════
// DECISIONS TABLE
// ═══════════════════════════════════════════════════
function updateDecisions() {
  const body = $('decBody');
  const countEl = $('decCount');
  if (!Array.isArray(decisions) || !decisions.length) {
    body.innerHTML = '<tr><td colspan="7" class="no-data">No decisions yet</td></tr>';
    countEl.textContent = '0 approved / 0 rejected';
    return;
  }
  const approved = decisions.filter(d=>d.decision==='APPROVED').length;
  const rejected = decisions.filter(d=>d.decision==='REJECTED').length;
  countEl.textContent = approved+' approved / '+rejected+' rejected';

  const sorted = [...decisions].reverse().slice(0,50);
  body.innerHTML = sorted.map(d => {
    const t = new Date(d.timestamp);
    const time = t.toLocaleString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    const isA = d.decision==='APPROVED';
    const dir = d.signal_direction||'--';
    const score = d.confluence_score != null ? fmt(d.confluence_score,3) : '\u2014';
    const modifier = d.modifier != null ? fmt(d.modifier,2)+'x' : '\u2014';
    const reason = d.rejection_stage || (isA ? 'Signal approved' : '\u2014');
    return '<tr>'
      +'<td class="td-time">'+time+'</td>'
      +'<td class="td-dir '+(dir==='LONG'?'long':'short')+'">'+dir+'</td>'
      +'<td class="td-price">'+fmtComma(d.price_at_signal||0)+'</td>'
      +'<td><span class="badge-dec '+(isA?'approved':'rejected')+'">'+(isA?'APPROVED':'REJECTED')+'</span></td>'
      +'<td class="td-score">'+score+'</td>'
      +'<td class="td-mod">'+modifier+'</td>'
      +'<td class="td-reason" title="'+reason.replace(/"/g,'&quot;')+'">'+reason+'</td>'
      +'</tr>';
  }).join('');
}

// ═══════════════════════════════════════════════════
// CHART ENGINE
// ═══════════════════════════════════════════════════
const canvas = $('chartCanvas');
const ctx = canvas.getContext('2d');

function getChartCandles() {
  let cc = (selectedTF === '2m') ? candles : (historicalData[selectedTF] || []);
  if (selectedTF === '2m' && historicalData['2m'] && historicalData['2m'].length) {
    const liveSet = new Set(candles.map(c=>c.time));
    const merged = [...historicalData['2m'].filter(c=>!liveSet.has(c.time)), ...candles];
    merged.sort((a,b)=>new Date(a.time)-new Date(b.time));
    cc = merged;
  }
  return cc;
}

function resizeCanvas() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.floor(rect.width);
  const h = Math.floor(rect.height);
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  // Offscreen double buffer
  if (!offscreen || offscreen.width !== canvas.width || offscreen.height !== canvas.height) {
    offscreen = document.createElement('canvas');
    offscreen.width = canvas.width;
    offscreen.height = canvas.height;
    offCtx = offscreen.getContext('2d');
  }
}

function scheduleRender() {
  if (!rafId) rafId = requestAnimationFrame(renderFrame);
}

function renderFrame() {
  rafId = 0;
  resizeCanvas();
  drawChart();
}

// Throttled mouse handler
let lastMouseTime = 0;
canvas.addEventListener('mousemove', e => {
  const now = performance.now();
  if (now - lastMouseTime < 16) return; // ~60fps
  lastMouseTime = now;
  const rect = canvas.getBoundingClientRect();
  mouseX = e.clientX - rect.left;
  mouseY = e.clientY - rect.top;
  scheduleRender();
});
canvas.addEventListener('mouseleave', () => {
  mouseX = -1; mouseY = -1; hoverIdx = -1;
  updateOHLC(-1);
  scheduleRender();
});

window.addEventListener('resize', () => scheduleRender());

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.width / dpr;
  const H = canvas.height / dpr;
  const c = offCtx;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);

  const chartCandles = getChartCandles();
  if (!chartCandles.length) {
    c.fillStyle = '#4a5568';
    c.font = '12px ' + FONT;
    c.textAlign = 'center';
    c.fillText('Waiting for candle data...', W/2, H/2);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(offscreen, 0, 0);
    return;
  }

  const visible = chartCandles.slice(-MAX_CANDLES);
  const n = visible.length;
  const chartW = W - PAD_LEFT - PAD_RIGHT;
  const chartH = H - PAD_TOP - PAD_BOTTOM;
  const volH = chartH * VOL_RATIO;
  const priceH = chartH - volH;
  const candleW = chartW / n;
  const bodyW = Math.max(1, Math.min(candleW * 0.7, 12));
  const wickW = 1;

  // Price range
  let hi = -Infinity, lo = Infinity, maxVol = 0;
  for (const bar of visible) {
    if (bar.h > hi) hi = bar.h;
    if (bar.l < lo) lo = bar.l;
    if ((bar.vol||0) > maxVol) maxVol = bar.vol||0;
  }
  const rawRange = hi - lo || 1;
  const padPx = rawRange * 0.06;
  hi += padPx; lo -= padPx;
  const priceRange = hi - lo;

  const priceY = p => PAD_TOP + (1 - (p - lo) / priceRange) * priceH;
  const candleCX = i => PAD_LEFT + i * candleW + candleW / 2;

  // ── RTH session shading ──
  drawSessionShading(c, visible, candleCX, candleW, n, PAD_TOP, priceH, W);

  // ── Grid lines ──
  const interval = niceInterval(priceRange, 6);
  const firstGrid = Math.ceil(lo / interval) * interval;
  c.lineWidth = 1;
  c.font = '10px ' + FONT;
  c.textAlign = 'right';
  for (let p = firstGrid; p <= hi; p += interval) {
    const y = Math.round(priceY(p)) + 0.5;
    c.strokeStyle = '#141c28';
    c.beginPath(); c.moveTo(PAD_LEFT, y); c.lineTo(W - PAD_RIGHT, y); c.stroke();
    c.fillStyle = '#4a5568';
    c.fillText(fmtComma(p), W - PAD_RIGHT + PAD_RIGHT - 4, y + 3);
  }

  // Time axis gridlines
  const timeStep = Math.max(1, Math.floor(n / 8));
  c.textAlign = 'center';
  let prevDate = '';
  for (let i = 0; i < n; i += timeStep) {
    const bar = visible[i];
    const t = new Date(bar.time);
    const x = Math.round(candleCX(i)) + 0.5;
    c.strokeStyle = '#141c28';
    c.lineWidth = 1;
    c.beginPath(); c.moveTo(x, PAD_TOP); c.lineTo(x, PAD_TOP + chartH); c.stroke();
  }

  // ── Supply/Demand zones ──
  drawZones(c, visible, candleCX, priceY, n, candleW);

  // ── Volume bars ──
  const volBase = PAD_TOP + priceH + volH;
  for (let i = 0; i < n; i++) {
    const bar = visible[i];
    const vH = maxVol > 0 ? ((bar.vol||0) / maxVol) * volH : 0;
    const isUp = bar.c >= bar.o;
    c.fillStyle = isUp ? 'rgba(0,212,170,0.15)' : 'rgba(255,59,92,0.15)';
    const bw = Math.max(1, bodyW - 1);
    c.fillRect(candleCX(i) - bw/2, volBase - vH, bw, vH);
  }

  // ── Candles ──
  for (let i = 0; i < n; i++) {
    const bar = visible[i];
    const isUp = bar.c >= bar.o;
    const x = candleCX(i);
    const oY = priceY(bar.o), cY = priceY(bar.c);
    const hY = priceY(bar.h), lY = priceY(bar.l);
    const color = isUp ? '#00d4aa' : '#ff3b5c';

    // Wick - crisp 1px
    c.strokeStyle = color;
    c.lineWidth = wickW;
    const wx = Math.round(x) + 0.5;
    c.beginPath(); c.moveTo(wx, Math.round(hY)); c.lineTo(wx, Math.round(lY)); c.stroke();

    // Body - solid filled
    const top = Math.min(oY, cY);
    const bH = Math.max(1, Math.abs(oY - cY));
    c.fillStyle = color;
    c.fillRect(Math.round(x - bodyW/2), Math.round(top), Math.round(bodyW), Math.round(bH));
  }

  // ── Trade markers ──
  drawTradeMarkers(c, visible, candleCX, priceY, n);

  // ── Current price line ──
  if (visible.length) {
    const last = visible[n-1];
    const cpY = priceY(last.c);
    const isUp = last.c >= last.o;
    const color = isUp ? '#00d4aa' : '#ff3b5c';
    c.setLineDash([4, 3]);
    c.strokeStyle = color;
    c.lineWidth = 1;
    c.beginPath(); c.moveTo(PAD_LEFT, Math.round(cpY)+0.5); c.lineTo(W - PAD_RIGHT, Math.round(cpY)+0.5); c.stroke();
    c.setLineDash([]);
    // Badge
    c.fillStyle = color;
    const bw = PAD_RIGHT - 4;
    c.fillRect(W - PAD_RIGHT + 2, cpY - 9, bw, 18);
    c.fillStyle = '#0a0e14';
    c.font = 'bold 10px ' + FONT;
    c.textAlign = 'center';
    c.fillText(fmtComma(last.c), W - PAD_RIGHT + 2 + bw/2, cpY + 4);
  }

  // ── Time axis labels ──
  c.font = '9px ' + FONT;
  c.textAlign = 'center';
  c.fillStyle = '#4a5568';
  prevDate = '';
  for (let i = 0; i < n; i += timeStep) {
    const bar = visible[i];
    const t = new Date(bar.time);
    const et = toET(t);
    const x = candleCX(i);
    const timeLabel = pad2(et.getHours()) + ':' + pad2(et.getMinutes());
    const dateStr = MONS[et.getMonth()] + ' ' + et.getDate();
    c.fillStyle = '#4a5568';
    if (dateStr !== prevDate && i > 0) {
      c.fillText(dateStr, x, H - 2);
      c.fillText(timeLabel, x, H - 13);
      prevDate = dateStr;
    } else {
      c.fillText(timeLabel, x, H - 7);
      if (i === 0) prevDate = dateStr;
    }
  }

  // ── Crosshair ──
  const inChart = mouseX >= PAD_LEFT && mouseX <= W - PAD_RIGHT && mouseY >= PAD_TOP && mouseY <= PAD_TOP + priceH;
  if (inChart) {
    const ci = Math.floor((mouseX - PAD_LEFT) / candleW);
    hoverIdx = Math.max(0, Math.min(ci, n-1));
    const hc = visible[hoverIdx];
    const cx = candleCX(hoverIdx);

    // Vertical line (snap to candle center)
    c.setLineDash([4, 4]);
    c.strokeStyle = 'rgba(77,166,255,0.3)';
    c.lineWidth = 1;
    c.beginPath(); c.moveTo(Math.round(cx)+0.5, PAD_TOP); c.lineTo(Math.round(cx)+0.5, PAD_TOP + priceH); c.stroke();

    // Horizontal line (follows mouse Y)
    c.strokeStyle = 'rgba(77,166,255,0.4)';
    c.beginPath(); c.moveTo(PAD_LEFT, Math.round(mouseY)+0.5); c.lineTo(W - PAD_RIGHT, Math.round(mouseY)+0.5); c.stroke();
    c.setLineDash([]);

    // Price badge at mouse Y on right axis
    const hoverPrice = hi - ((mouseY - PAD_TOP) / priceH) * priceRange;
    const pbw = PAD_RIGHT - 4;
    c.fillStyle = '#2563eb';
    c.fillRect(W - PAD_RIGHT + 2, mouseY - 9, pbw, 18);
    c.fillStyle = '#ffffff';
    c.font = '10px ' + FONT;
    c.textAlign = 'center';
    c.fillText(fmtComma(hoverPrice), W - PAD_RIGHT + 2 + pbw/2, mouseY + 4);

    // Time badge at candle X on bottom axis
    const ht = new Date(hc.time);
    const het = toET(ht);
    const tStr = DAYS[het.getDay()] + ' ' + MONS[het.getMonth()] + ' ' + pad2(het.getDate()) + '  ' + pad2(het.getHours()) + ':' + pad2(het.getMinutes());
    c.font = '10px ' + FONT;
    const tw = c.measureText(tStr).width + 14;
    c.fillStyle = '#2563eb';
    c.fillRect(cx - tw/2, H - PAD_BOTTOM, tw, PAD_BOTTOM);
    c.fillStyle = '#ffffff';
    c.textAlign = 'center';
    c.fillText(tStr, cx, H - PAD_BOTTOM + 15);

    updateOHLC(hoverIdx, visible);
  } else if (mouseX < 0) {
    updateOHLC(-1);
  }

  // Copy offscreen to visible canvas
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(offscreen, 0, 0);
}

// ── Session shading (RTH) ──
function drawSessionShading(c, visible, candleCX, candleW, n, top, height, W) {
  for (let i = 0; i < n; i++) {
    const t = new Date(visible[i].time);
    if (isRTH(t)) {
      c.fillStyle = 'rgba(77,166,255,0.03)';
      c.fillRect(candleCX(i) - candleW/2, top, candleW, height);
    }
  }
}

// ── Supply/Demand zones ──
function drawZones(c, visible, candleCX, priceY, n, candleW) {
  if (visible.length < 5) return;
  for (let i = 2; i < n - 2; i++) {
    const bar = visible[i];
    // Supply (swing high)
    if (bar.h > visible[i-1].h && bar.h > visible[i-2].h && bar.h > visible[i+1].h && bar.h > visible[i+2].h) {
      const y1 = priceY(bar.h);
      const y2 = priceY(Math.max(bar.o, bar.c));
      const x1 = candleCX(i) - candleW/2;
      const x2 = candleCX(n-1) + candleW/2;
      c.fillStyle = 'rgba(255,59,92,0.06)';
      c.fillRect(x1, y1, x2 - x1, y2 - y1);
      c.strokeStyle = 'rgba(255,59,92,0.20)';
      c.lineWidth = 1;
      c.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
    // Demand (swing low)
    if (bar.l < visible[i-1].l && bar.l < visible[i-2].l && bar.l < visible[i+1].l && bar.l < visible[i+2].l) {
      const y1 = priceY(Math.min(bar.o, bar.c));
      const y2 = priceY(bar.l);
      const x1 = candleCX(i) - candleW/2;
      const x2 = candleCX(n-1) + candleW/2;
      c.fillStyle = 'rgba(0,212,170,0.06)';
      c.fillRect(x1, y1, x2 - x1, y2 - y1);
      c.strokeStyle = 'rgba(0,212,170,0.20)';
      c.lineWidth = 1;
      c.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
  }
}

// ── Trade markers ──
function drawTradeMarkers(c, visible, candleCX, priceY, n) {
  if (!Array.isArray(activeTrades)) return;
  for (const trade of activeTrades) {
    if (!trade.ep || !trade.entry_time) continue;
    let entryIdx = -1;
    const entryTime = new Date(trade.entry_time).getTime();
    for (let i = 0; i < n; i++) {
      if (Math.abs(new Date(visible[i].time).getTime() - entryTime) < 130000) { entryIdx = i; break; }
    }
    if (entryIdx < 0) continue;

    const x = candleCX(entryIdx);
    const y = priceY(trade.ep);
    const isLong = (trade.dir||'').toUpperCase() === 'LONG';
    const color = isLong ? '#00d4aa' : '#ff3b5c';

    // Entry arrow
    c.fillStyle = color;
    c.beginPath();
    if (isLong) { c.moveTo(x, y-2); c.lineTo(x-5, y+8); c.lineTo(x+5, y+8); }
    else { c.moveTo(x, y+2); c.lineTo(x-5, y-8); c.lineTo(x+5, y-8); }
    c.fill();

    // Exit info
    if (trade.exit_price && trade.exit_time) {
      let exitIdx = -1;
      const exitTime = new Date(trade.exit_time).getTime();
      for (let i = 0; i < n; i++) {
        if (Math.abs(new Date(visible[i].time).getTime() - exitTime) < 130000) { exitIdx = i; break; }
      }
      if (exitIdx >= 0) {
        const ex = candleCX(exitIdx);
        const ey = priceY(trade.exit_price);
        const pnl = trade.unrealized_pnl || 0;
        const pColor = pnl >= 0 ? '#00d4aa' : '#ff3b5c';

        // Connection line
        c.setLineDash([4, 4]);
        c.strokeStyle = pColor;
        c.lineWidth = 1;
        c.beginPath(); c.moveTo(x, y); c.lineTo(ex, ey); c.stroke();
        c.setLineDash([]);

        // Exit circle
        c.fillStyle = pColor;
        c.beginPath(); c.arc(ex, ey, 3, 0, Math.PI*2); c.fill();

        // PnL label
        const holdMin = Math.round((exitTime - entryTime) / 60000);
        const label = (pnl>=0?'+':'')+'\u0024'+pnl.toFixed(0)+' \u00b7 '+holdMin+'m';
        c.font = 'bold 9px ' + FONT;
        const tw = c.measureText(label).width + 8;
        const mx = (x+ex)/2, my = (y+ey)/2 - 10;
        c.fillStyle = pnl >= 0 ? 'rgba(0,212,170,0.85)' : 'rgba(255,59,92,0.85)';
        c.fillRect(mx - tw/2, my - 7, tw, 14);
        c.fillStyle = '#ffffff';
        c.textAlign = 'center';
        c.fillText(label, mx, my + 3);
      }
    }
  }
}

// ── OHLCV Overlay update ──
function updateOHLC(idx, visible) {
  const src = getChartCandles();
  const data = (idx >= 0 && visible) ? visible[idx] : (src.length ? src[src.length-1] : null);
  if (!data) return;
  const chg = data.c - data.o;
  const pct = data.o ? (chg/data.o*100) : 0;
  const up = chg >= 0;
  const cls = up ? 'ohlc-up' : 'ohlc-dn';

  $('oO').textContent = fmtComma(data.o);
  $('oO').className = cls;
  $('oH').textContent = fmtComma(data.h);
  $('oH').className = 'ohlc-up';
  $('oL').textContent = fmtComma(data.l);
  $('oL').className = 'ohlc-dn';
  $('oC').textContent = fmtComma(data.c);
  $('oC').className = cls;
  $('oChg').textContent = ' ' + fmtPnl(chg) + ' (' + fmtPnl(pct) + '%)';
  $('oChg').className = cls;
  $('oVol').textContent = data.vol ? '  Vol ' + (data.vol||0) : '';
  $('ohlcDot').className = up ? 'ohlc-up' : 'ohlc-dn';
}

// ═══════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════
fetchData();
setInterval(fetchData, 3000);
scheduleRender();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# HTTP REQUEST HANDLER
# ═══════════════════════════════════════════════════════════════

class DashboardHandler(BaseHTTPRequestHandler):
    """Serves dashboard HTML and JSON API endpoints."""

    def log_message(self, format, *args):
        """Suppress default access logging."""
        pass

    def _send_json(self, data, status_code=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(DASHBOARD_HTML)
            return

        if path == "/api/historical":
            params = parse_qs(parsed.query)
            tf = params.get("timeframe", ["2m"])[0]
            # Sanitize timeframe to prevent path traversal
            allowed_tfs = {"1m", "2m", "5m", "15m", "30m", "1H", "4H", "1D"}
            if tf not in allowed_tfs:
                self._send_json([])
                return
            filepath = LOGS_DIR / f"historical_bars_{tf}.json"
            data = _read_json_file(filepath, "candles", False) if filepath.exists() else []
            self._send_json(data)
            return

        if path in FILE_MAP:
            filename, default_key, is_jsonl = FILE_MAP[path]
            filepath = LOGS_DIR / filename
            data = _read_json_file(filepath, default_key, is_jsonl)
            self._send_json(data)
            return

        self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ═══════════════════════════════════════════════════════════════
# SERVER
# ═══════════════════════════════════════════════════════════════

class DashboardServer:
    """Threaded HTTP server for the live dashboard."""

    def __init__(self, port: int = 8080):
        self.port = port
        self._server = None
        self._thread = None

    def start(self, blocking: bool = False):
        """Start the dashboard server."""
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        self._server = HTTPServer(("0.0.0.0", self.port), DashboardHandler)
        logger.info("Dashboard server starting on http://localhost:%d", self.port)

        if blocking:
            self._server.serve_forever()
        else:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="dashboard-server",
            )
            self._thread.start()

    def stop(self):
        """Stop the dashboard server."""
        if self._server:
            self._server.shutdown()
            logger.info("Dashboard server stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Live Paper Trading Dashboard Server")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    server = DashboardServer(port=args.port)
    print(f"  Dashboard: http://localhost:{args.port}")
    print("  Press Ctrl+C to stop")

    try:
        server.start(blocking=True)
    except KeyboardInterrupt:
        print("\n  Shutting down dashboard server...")
        server.stop()


if __name__ == "__main__":
    main()
