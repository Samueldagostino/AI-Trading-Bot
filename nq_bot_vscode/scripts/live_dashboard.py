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
import time
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


def _get_gamma_data() -> dict:
    """Read latest GEX data from gamma_levels.json for the /api/gamma endpoint."""
    default = {
        "enabled": False,
        "regime": "UNKNOWN",
        "net_gex": 0,
        "net_gex_display": "N/A",
        "gamma_flip": None,
        "call_wall": None,
        "put_wall": None,
        "modifier_value": 1.0,
        "last_update": None,
    }
    gamma_path = LOGS_DIR / "gamma_levels.json"
    modifier_path = LOGS_DIR / "modifier_state.json"

    try:
        # Read latest gamma levels entry
        if gamma_path.exists():
            text = gamma_path.read_text(encoding="utf-8").strip()
            if text:
                lines = text.split("\n")
                last_line = lines[-1].strip()
                if last_line:
                    entry = json.loads(last_line)
                    # Prefer QQQ (MNQ relevance), fall back to SPY
                    data = entry.get("qqq", entry.get("spy", {}))
                    if data:
                        default["enabled"] = True
                        default["regime"] = data.get("regime", "UNKNOWN")
                        default["net_gex"] = data.get("net_gex", 0)
                        default["net_gex_display"] = data.get("net_gex_display", "N/A")
                        default["gamma_flip"] = data.get("gamma_flip_strike")
                        default["call_wall"] = data.get("nearest_call_wall")
                        default["put_wall"] = data.get("nearest_put_wall")
                        default["last_update"] = data.get("timestamp")

        # Read modifier value from modifier_state
        if modifier_path.exists():
            mod_text = modifier_path.read_text(encoding="utf-8").strip()
            if mod_text:
                mod_data = json.loads(mod_text)
                default["modifier_value"] = mod_data.get("gamma", {}).get("value", 1.0)

    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.warning("Error reading gamma data: %s", e)

    return default


def atomic_write_json(filepath: Path, data) -> None:
    """Write JSON atomically: write to .tmp then rename (Windows retry)."""
    tmp_path = filepath.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        # Windows PermissionError retry — another process may hold the file briefly
        for attempt in range(3):
            try:
                os.replace(str(tmp_path), str(filepath))
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.1)
                else:
                    raise
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
<title>NQ.BOT - Live Trading Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
--bg-primary:#0a0e14;--bg-secondary:#0d1117;--bg-panel:#0b1018;
--border:#1a2332;--grid:#141c28;
--text-primary:#e2e8f0;--text-secondary:#6b7a8d;--text-muted:#3d4a5c;
--green:#00d4aa;--green-fill:rgba(0,212,170,0.12);
--red:#ff3b5c;--red-fill:rgba(255,59,92,0.12);
--amber:#ffb800;--amber-fill:rgba(255,184,0,0.12);
--blue:#4da6ff;--blue-fill:rgba(77,166,255,0.12);
}
html,body{height:100%;overflow:hidden}
body{
  font-family:'JetBrains Mono','Fira Code','SF Mono','Cascadia Code',monospace;
  background:var(--bg-primary);color:var(--text-primary);
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  display:flex;flex-direction:column;
}
/* === HEADER === */
.header{
  height:40px;display:flex;align-items:center;padding:0 12px;
  background:var(--bg-panel);border-bottom:1px solid var(--border);
  flex-shrink:0;gap:10px;
}
.brand{display:flex;align-items:center;gap:2px;margin-right:6px}
.brand-nq{color:var(--green);font-weight:700;font-size:16px}
.brand-bot{color:#4a5568;font-size:16px}
.paper-badge{
  font-size:9px;text-transform:uppercase;letter-spacing:1px;
  background:rgba(255,184,0,0.15);border:1px solid rgba(255,184,0,0.4);
  color:var(--amber);padding:1px 6px;border-radius:3px;margin-right:6px;
}
.header-price{display:flex;align-items:baseline;gap:6px;margin-right:10px}
.header-sym{color:#6b7a8d;font-size:12px}
.header-val{color:#fff;font-weight:700;font-size:18px}
.header-chg{font-size:12px}
.tf-group{display:flex;gap:3px;margin-left:auto;margin-right:12px}
.tf-btn{
  width:36px;height:22px;font-size:10px;font-family:inherit;
  background:transparent;color:#6b7a8d;border:1px solid var(--border);
  border-radius:3px;cursor:pointer;display:flex;align-items:center;justify-content:center;
}
.tf-btn:hover{background:#1a2332}
.tf-btn.active{background:rgba(0,212,170,0.15);color:var(--green);border-color:var(--green)}
.live-ind{display:flex;align-items:center;gap:5px;margin-right:10px}
.live-dot{
  width:8px;height:8px;border-radius:50%;background:var(--green);
  box-shadow:0 0 6px var(--green);
  animation:pulse 2s ease-in-out infinite;
}
.live-dot.disconnected{background:var(--red);box-shadow:0 0 6px var(--red);animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.live-text{font-size:10px;color:var(--green);text-transform:uppercase;letter-spacing:1px}
.live-text.disconnected{color:var(--red)}
.header-clock{font-size:12px;color:#6b7a8d;font-family:inherit}
/* === STATS BAR === */
.stats-bar{
  height:48px;display:flex;padding:6px 16px;gap:4px;
  background:var(--bg-panel);border-bottom:1px solid var(--border);flex-shrink:0;
}
.stat-card{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
}
.stat-label{font-size:8px;text-transform:uppercase;color:#4a5568;letter-spacing:1.5px}
.stat-value{font-size:20px;font-weight:700}
.stat-sub{font-size:8px;color:#3d4a5c}
/* === MAIN AREA === */
.main-area{flex:1;display:flex;overflow:hidden}
.chart-col{flex:1;position:relative;background:var(--bg-secondary);overflow:hidden;display:flex;flex-direction:column}
.chart-wrap{flex:1;position:relative;overflow:hidden}
#chartCanvas{position:absolute;top:0;left:0;width:100%;height:100%}
/* === SIDEBAR === */
.sidebar{
  width:240px;background:var(--bg-panel);border-left:1px solid var(--border);
  display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0;
}
.sb-panel{padding:10px 12px;border-bottom:1px solid var(--border)}
.sb-header{font-size:10px;text-transform:uppercase;color:#4a5568;letter-spacing:1.5px;margin-bottom:8px}
.pos-card{
  padding:8px;border-radius:4px;margin-bottom:6px;
}
.pos-card.long{background:rgba(0,212,170,0.08);border:1px solid rgba(0,212,170,0.25)}
.pos-card.short{background:rgba(255,59,92,0.08);border:1px solid rgba(255,59,92,0.25)}
.pos-dir{font-size:10px;font-weight:700;margin-bottom:2px}
.pos-entry{font-size:11px;color:var(--text-primary)}
.pos-pnl{font-size:14px;font-weight:700;margin:2px 0}
.pos-hold{font-size:10px;font-weight:700;color:var(--amber)}
.pos-mod{font-size:8px;color:#4a5568}
.pos-empty{font-size:10px;color:#3d4a5c;text-align:center;padding:12px 0}
/* Safety rails */
.rail-row{margin-bottom:8px}
.rail-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}
.rail-label{font-size:9px;color:#6b7a8d}
.rail-status{font-size:9px;font-weight:700}
.rail-bar{height:3px;background:#141c28;border-radius:2px;overflow:hidden}
.rail-fill{height:100%;border-radius:2px;transition:width 0.5s}
.rail-val{font-size:8px;color:#3d4a5c;margin-top:2px}
/* Modifiers */
.mod-row{padding:6px 0;border-bottom:1px solid var(--border)}
.mod-row:last-child{border-bottom:none}
.mod-top{display:flex;justify-content:space-between;align-items:center}
.mod-name{font-size:9px;color:#6b7a8d}
.mod-val{font-size:11px}
.mod-reason{font-size:7px;color:#3d4a5c;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px}
.mod-total{display:flex;justify-content:space-between;align-items:center;padding-top:8px;margin-top:4px;border-top:1px solid var(--border)}
.mod-total-label{font-size:10px;font-weight:700;color:#6b7a8d}
.mod-total-val{font-size:14px;font-weight:700;color:var(--blue)}
/* === DECISIONS TABLE === */
.decisions-panel{
  height:200px;flex-shrink:0;background:var(--bg-panel);
  border-top:1px solid var(--border);display:flex;flex-direction:column;
}
.dec-header{
  display:flex;justify-content:space-between;align-items:center;
  padding:6px 12px;flex-shrink:0;
}
.dec-title{font-size:10px;font-weight:700;color:#4a5568;text-transform:uppercase;letter-spacing:1px}
.dec-summary{font-size:9px;color:#4a5568}
.dec-table-wrap{flex:1;overflow-y:auto;padding:0 12px}
.dec-table-wrap::-webkit-scrollbar{width:6px}
.dec-table-wrap::-webkit-scrollbar-track{background:#1a2332}
.dec-table-wrap::-webkit-scrollbar-thumb{background:#3d4a5c;border-radius:3px}
table.dec-table{width:100%;border-collapse:collapse;font-size:10px}
table.dec-table thead th{
  font-size:8px;text-transform:uppercase;color:#3d4a5c;letter-spacing:1.2px;
  padding:4px 6px;text-align:left;position:sticky;top:0;
  background:var(--bg-panel);border-bottom:1px solid var(--border);
}
table.dec-table tbody tr{height:28px}
table.dec-table tbody tr:nth-child(even){background:rgba(13,17,23,0.3)}
table.dec-table td{padding:4px 6px;white-space:nowrap}
.td-time{color:#6b7a8d;font-family:inherit}
.td-dir{font-size:10px;font-weight:700}
.td-price{color:var(--text-primary)}
.td-decision{font-size:8px;font-weight:700;padding:1px 6px;border-radius:3px;display:inline-block}
.td-decision.approved{background:rgba(0,212,170,0.2);color:var(--green)}
.td-decision.rejected{background:rgba(255,59,92,0.2);color:var(--red)}
.td-score{color:#6b7a8d}
.td-mod{color:#6b7a8d}
.td-reason{color:#6b7a8d;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* === STATUS BAR === */
.status-bar{
  height:20px;display:flex;align-items:center;justify-content:flex-end;
  padding:0 12px;background:var(--bg-panel);border-top:1px solid var(--border);
  flex-shrink:0;
}
.status-text{font-size:9px;color:#4a5568;font-family:inherit}
/* === RESPONSIVE === */
@media(max-width:1280px){.sidebar{display:none}}
</style>
</head>
<body>
<!-- HEADER -->
<div class="header">
  <div class="brand"><span class="brand-nq">NQ</span><span class="brand-bot">.BOT</span></div>
  <span class="paper-badge">PAPER</span>
  <div class="header-price">
    <span class="header-sym">MNQ</span>
    <span class="header-val" id="hPrice">--</span>
    <span class="header-chg" id="hChange">--</span>
  </div>
  <div class="tf-group" id="tfGroup">
    <button class="tf-btn" data-tf="1m">1m</button>
    <button class="tf-btn active" data-tf="2m">2m</button>
    <button class="tf-btn" data-tf="5m">5m</button>
    <button class="tf-btn" data-tf="15m">15m</button>
    <button class="tf-btn" data-tf="30m">30m</button>
    <button class="tf-btn" data-tf="1H">1H</button>
    <button class="tf-btn" data-tf="4H">4H</button>
    <button class="tf-btn" data-tf="1D">1D</button>
  </div>
  <div class="live-ind">
    <div class="live-dot" id="liveDot"></div>
    <span class="live-text" id="liveText">LIVE</span>
  </div>
  <span class="header-clock" id="hClock">--:--:-- ET</span>
</div>
<!-- STATS BAR -->
<div class="stats-bar" id="statsBar">
  <div class="stat-card"><div class="stat-label">TRADES</div><div class="stat-value" id="sTrades">0</div></div>
  <div class="stat-card"><div class="stat-label">WIN RATE</div><div class="stat-value" id="sWinRate">0%</div></div>
  <div class="stat-card"><div class="stat-label">PNL</div><div class="stat-value" id="sPnl">$0</div></div>
  <div class="stat-card"><div class="stat-label">PROFIT FACTOR</div><div class="stat-value" id="sPF">0.00</div></div>
  <div class="stat-card"><div class="stat-label">SHARPE</div><div class="stat-value" id="sSharpe">0.00</div></div>
  <div class="stat-card"><div class="stat-label">MAX DD</div><div class="stat-value" id="sDD">0.0%</div></div>
  <div class="stat-card"><div class="stat-label">CURR DD</div><div class="stat-value" id="sCDD">0.0%</div></div>
</div>
<!-- MAIN AREA -->
<div class="main-area">
  <div class="chart-col">
    <div class="chart-wrap" id="chartWrap">
      <canvas id="chartCanvas"></canvas>
    </div>
  </div>
  <div class="sidebar" id="sidebar">
    <div class="sb-panel" id="posPanel">
      <div class="sb-header">ACTIVE POSITIONS</div>
      <div id="posContent"><div class="pos-empty">No open positions</div></div>
    </div>
    <div class="sb-panel" id="safetyPanel">
      <div class="sb-header">SAFETY RAILS</div>
      <div id="safetyContent"></div>
    </div>
    <div class="sb-panel" id="modPanel">
      <div class="sb-header">MODIFIERS</div>
      <div id="modContent"></div>
    </div>
  </div>
</div>
<!-- DECISIONS TABLE -->
<div class="decisions-panel">
  <div class="dec-header">
    <span class="dec-title">TRADE DECISIONS</span>
    <span class="dec-summary" id="decSummary"></span>
  </div>
  <div class="dec-table-wrap">
    <table class="dec-table">
      <thead><tr>
        <th>TIME</th><th>DIR</th><th>PRICE</th><th>DECISION</th><th>SCORE</th><th>MODIFIER</th><th>REASON</th>
      </tr></thead>
      <tbody id="decBody"></tbody>
    </table>
  </div>
</div>
<!-- STATUS BAR -->
<div class="status-bar">
  <span class="status-text" id="statusText"></span>
</div>

<script>
// ═══════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════
const S = {
  candles: [], trades: [], decisions: [], status: {},
  modifiers: {}, safety: {}, gamma: {},
  activeTimeframe: '2m',
  failCount: 0, lastData: {},
  mouse: { x: -1, y: -1, over: false },
  chartDirty: true,
};

// ═══════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════
const fmtComma = (n) => {
  if (n == null || isNaN(n)) return '--';
  const parts = Number(n).toFixed(2).split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return parts.join('.');
};
const fmtPnl = (n) => {
  if (n == null || isNaN(n)) return '$0.00';
  const sign = n >= 0 ? '+' : '';
  return sign + '$' + fmtComma(Math.abs(n));
};
const pnlColor = (n) => n >= 0 ? 'var(--green)' : 'var(--red)';
const fmtTime = (iso) => {
  if (!iso) return '--';
  const d = new Date(iso);
  if (isNaN(d)) return '--';
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
};
const fmtHold = (sec) => {
  if (!sec || sec < 0) return '00:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
};

// ═══════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════
const updateClock = () => {
  const now = new Date();
  const etStr = now.toLocaleString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  document.getElementById('hClock').textContent = etStr + ' ET';

  const utcOff = -5;
  const etNow = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const h = etNow.getHours();
  const m = etNow.getMinutes();
  const mins = h * 60 + m;
  const isRTH = mins >= 570 && mins < 960;

  const utcStr = now.toLocaleString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  document.getElementById('statusText').textContent = utcStr + ' UTC-5  ' + (isRTH ? 'RTH' : 'ETH');
};
setInterval(updateClock, 1000);
updateClock();

// ═══════════════════════════════════════════════════
// DATA FETCHING
// ═══════════════════════════════════════════════════
const fetchJSON = async (url) => {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(r.status);
  return r.json();
};

const fetchAllData = async () => {
  try {
    const endpoints = [
      fetchJSON('/api/candles'),
      fetchJSON('/api/status'),
      fetchJSON('/api/decisions'),
      fetchJSON('/api/trades'),
      fetchJSON('/api/modifiers'),
      fetchJSON('/api/safety'),
      fetchJSON('/api/gamma'),
    ];
    const [candles, status, decisions, trades, modifiers, safety, gamma] = await Promise.all(endpoints);
    S.candles = Array.isArray(candles) ? candles : [];
    S.status = status || {};
    S.decisions = Array.isArray(decisions) ? decisions : [];
    S.trades = Array.isArray(trades) ? trades : [];
    S.modifiers = modifiers || {};
    S.safety = safety || {};
    S.gamma = gamma || {};
    S.failCount = 0;
    setLiveStatus(true);
    updateAll();
  } catch (e) {
    S.failCount++;
    if (S.failCount >= 3) setLiveStatus(false);
  }
};

const fetchTimeframe = async (tf) => {
  try {
    const data = await fetchJSON('/api/historical?timeframe=' + tf);
    S.candles = Array.isArray(data) ? data : [];
    S.chartDirty = true;
    renderChart();
  } catch (e) { /* keep existing data */ }
};

const setLiveStatus = (ok) => {
  const dot = document.getElementById('liveDot');
  const txt = document.getElementById('liveText');
  if (ok) {
    dot.className = 'live-dot';
    txt.className = 'live-text';
    txt.textContent = 'LIVE';
  } else {
    dot.className = 'live-dot disconnected';
    txt.className = 'live-text disconnected';
    txt.textContent = 'DISCONNECTED';
  }
};

// ═══════════════════════════════════════════════════
// TIMEFRAME BUTTONS
// ═══════════════════════════════════════════════════
document.getElementById('tfGroup').addEventListener('click', (e) => {
  const btn = e.target.closest('.tf-btn');
  if (!btn) return;
  const tf = btn.dataset.tf;
  document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  S.activeTimeframe = tf;
  if (tf === '2m') {
    fetchAllData();
  } else {
    fetchTimeframe(tf);
  }
});

// ═══════════════════════════════════════════════════
// UPDATE UI
// ═══════════════════════════════════════════════════
const updateAll = () => {
  updateHeader();
  updateStats();
  updatePositions();
  updateSafety();
  updateModifiers();
  updateDecisions();
  S.chartDirty = true;
  renderChart();
};

const updateHeader = () => {
  const c = S.candles;
  if (c.length < 2) return;
  const last = c[c.length - 1];
  const prev = c[c.length - 2];
  const price = last.c;
  const change = price - prev.c;
  const pct = prev.c ? ((change / prev.c) * 100) : 0;
  document.getElementById('hPrice').textContent = fmtComma(price);
  const chgEl = document.getElementById('hChange');
  const sign = change >= 0 ? '+' : '';
  chgEl.textContent = sign + change.toFixed(2) + ' (' + sign + pct.toFixed(2) + '%)';
  chgEl.style.color = change >= 0 ? 'var(--green)' : 'var(--red)';
};

const updateStats = () => {
  const s = S.status;
  document.getElementById('sTrades').textContent = s.trade_count || 0;
  const wr = (s.win_rate || 0);
  const wrEl = document.getElementById('sWinRate');
  wrEl.textContent = (wr * 100).toFixed(1) + '%';
  wrEl.style.color = wr > 0.5 ? 'var(--green)' : wr >= 0.4 ? 'var(--amber)' : 'var(--red)';

  const pnl = s.total_pnl || 0;
  const pnlEl = document.getElementById('sPnl');
  pnlEl.textContent = fmtPnl(pnl);
  pnlEl.style.color = pnlColor(pnl);

  const pf = s.profit_factor || 0;
  const pfEl = document.getElementById('sPF');
  pfEl.textContent = pf.toFixed(2);
  pfEl.style.color = pf > 1.5 ? 'var(--green)' : pf >= 1.0 ? 'var(--amber)' : 'var(--red)';

  document.getElementById('sSharpe').textContent = (s.sharpe_estimate || 0).toFixed(2);
  document.getElementById('sDD').textContent = ((s.max_drawdown || 0) * 100).toFixed(1) + '%';
  document.getElementById('sCDD').textContent = ((s.current_drawdown || 0) * 100).toFixed(1) + '%';
};

const updatePositions = () => {
  const el = document.getElementById('posContent');
  if (!S.trades || S.trades.length === 0) {
    el.innerHTML = '<div class="pos-empty">No open positions</div>';
    return;
  }
  let html = '';
  for (const t of S.trades) {
    const isLong = (t.dir || '').toUpperCase() === 'LONG';
    const cls = isLong ? 'long' : 'short';
    const pnl = t.unrealized_pnl || 0;
    const holdSec = t.entry_time ? Math.floor((Date.now() - new Date(t.entry_time).getTime()) / 1000) : 0;
    html += '<div class="pos-card ' + cls + '">'
      + '<div class="pos-dir" style="color:' + (isLong ? 'var(--green)' : 'var(--red)') + '">' + (t.dir || '?').toUpperCase() + '</div>'
      + '<div class="pos-entry">' + (t.contracts || 1) + 'x @ ' + fmtComma(t.ep) + '</div>'
      + '<div class="pos-pnl" style="color:' + pnlColor(pnl) + '">' + fmtPnl(pnl) + '</div>'
      + '<div class="pos-hold">HOLD ' + fmtHold(holdSec) + '</div>'
      + '<div class="pos-mod">Mod: ' + (t.modifier ? t.modifier.toFixed(2) + 'x' : '--') + '</div>'
      + '</div>';
  }
  el.innerHTML = html;
};

const updateSafety = () => {
  const s = S.safety;
  const rails = [
    { label: 'Daily Loss', val: Math.abs(s.daily_pnl || 0), max: s.daily_limit || 500, fmt: (v, m) => '$' + v.toFixed(0) + ' / $' + m.toFixed(0) },
    { label: 'Consec Losses', val: s.consec_losses || 0, max: s.max_consec || 5, fmt: (v, m) => v + ' / ' + m },
    { label: 'Position Size', val: s.position_size || 0, max: s.max_position || 2, fmt: (v, m) => v + ' / ' + m },
    { label: 'Heartbeat', val: Math.min(s.heartbeat_age_sec || 0, 60), max: 60, fmt: (v) => v.toFixed(0) + 's' },
  ];
  let html = '';
  for (const r of rails) {
    const pct = r.max > 0 ? Math.min((r.val / r.max) * 100, 100) : 0;
    const color = pct < 50 ? 'var(--green)' : pct < 80 ? 'var(--amber)' : 'var(--red)';
    const statusOk = pct < 80;
    html += '<div class="rail-row">'
      + '<div class="rail-top"><span class="rail-label">' + r.label + '</span>'
      + '<span class="rail-status" style="color:' + (statusOk ? 'var(--green)' : 'var(--red)') + '">' + (statusOk ? 'OK' : 'ALERT') + '</span></div>'
      + '<div class="rail-bar"><div class="rail-fill" style="width:' + pct + '%;background:' + color + '"></div></div>'
      + '<div class="rail-val">' + r.fmt(r.val, r.max) + '</div>'
      + '</div>';
  }
  document.getElementById('safetyContent').innerHTML = html;
};

const updateModifiers = () => {
  const m = S.modifiers;
  const mods = [
    { key: 'har_rv', label: 'HAR-RV' },
    { key: 'fomc', label: 'FOMC' },
    { key: 'overnight', label: 'OVERNIGHT' },
    { key: 'gamma', label: 'GAMMA' },
  ];
  let html = '';
  for (const mod of mods) {
    const data = m[mod.key] || { value: 1.0, reason: 'No data' };
    const v = data.value || 1.0;
    const color = v > 1.1 ? 'var(--green)' : v < 0.9 ? 'var(--amber)' : 'var(--text-muted)';
    html += '<div class="mod-row">'
      + '<div class="mod-top"><span class="mod-name">' + mod.label + '</span>'
      + '<span class="mod-val" style="color:' + color + '">' + v.toFixed(2) + 'x</span></div>'
      + '<div class="mod-reason">' + (data.reason || '') + '</div>'
      + '</div>';
  }
  const total = m.total || 1.0;
  html += '<div class="mod-total"><span class="mod-total-label">TOTAL</span>'
    + '<span class="mod-total-val">' + (typeof total === 'number' ? total.toFixed(2) : '1.00') + 'x</span></div>';
  document.getElementById('modContent').innerHTML = html;
};

const updateDecisions = () => {
  const decs = S.decisions || [];
  const approved = decs.filter(d => (d.decision || '').toUpperCase() === 'APPROVED').length;
  const rejected = decs.length - approved;
  document.getElementById('decSummary').textContent = approved + ' approved / ' + rejected + ' rejected';

  let html = '';
  const sorted = [...decs].reverse();
  for (const d of sorted.slice(0, 50)) {
    const dir = d.signal_direction || d.direction || '--';
    const isApproved = (d.decision || '').toUpperCase() === 'APPROVED';
    const score = d.score != null ? Number(d.score).toFixed(3) : (d.combined_score != null ? Number(d.combined_score).toFixed(3) : '--');
    const modifier = d.modifier != null ? Number(d.modifier).toFixed(2) + 'x' : '--';
    html += '<tr>'
      + '<td class="td-time">' + fmtTime(d.timestamp) + '</td>'
      + '<td class="td-dir" style="color:' + (dir.toUpperCase() === 'LONG' ? 'var(--green)' : 'var(--red)') + '">' + dir.toUpperCase() + '</td>'
      + '<td class="td-price">' + fmtComma(d.price_at_signal || d.price || 0) + '</td>'
      + '<td><span class="td-decision ' + (isApproved ? 'approved' : 'rejected') + '">' + (d.decision || '--').toUpperCase() + '</span></td>'
      + '<td class="td-score">' + score + '</td>'
      + '<td class="td-mod">' + modifier + '</td>'
      + '<td class="td-reason" title="' + ((d.reason || '').replace(/"/g, '&quot;')) + '">' + (d.reason || '--') + '</td>'
      + '</tr>';
  }
  document.getElementById('decBody').innerHTML = html;
};

// ═══════════════════════════════════════════════════
// CHART RENDERING
// ═══════════════════════════════════════════════════
const canvas = document.getElementById('chartCanvas');
const ctx = canvas.getContext('2d');
let offscreen = null;
let offCtx = null;
let chartW = 0, chartH = 0, dpr = 1;

const PRICE_AXIS_W = 70;
const TIME_AXIS_H = 28;
const VOLUME_PCT = 0.15;

const resizeCanvas = () => {
  const wrap = document.getElementById('chartWrap');
  const rect = wrap.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  chartW = rect.width;
  chartH = rect.height;
  canvas.width = chartW * dpr;
  canvas.height = chartH * dpr;
  canvas.style.width = chartW + 'px';
  canvas.style.height = chartH + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  offscreen = document.createElement('canvas');
  offscreen.width = chartW * dpr;
  offscreen.height = chartH * dpr;
  offCtx = offscreen.getContext('2d');
  offCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  S.chartDirty = true;
};

window.addEventListener('resize', resizeCanvas);
resizeCanvas();

// Mouse tracking
canvas.addEventListener('mousemove', (e) => {
  const rect = canvas.getBoundingClientRect();
  S.mouse.x = e.clientX - rect.left;
  S.mouse.y = e.clientY - rect.top;
  S.mouse.over = true;
  S.chartDirty = true;
});
canvas.addEventListener('mouseleave', () => {
  S.mouse.over = false;
  S.chartDirty = true;
});

const renderChart = () => {
  if (!offCtx || chartW < 10 || chartH < 10) return;
  const c = offCtx;
  const candles = S.candles;
  const plotW = chartW - PRICE_AXIS_W;
  const plotH = chartH - TIME_AXIS_H;

  // Clear
  c.fillStyle = '#0d1117';
  c.fillRect(0, 0, chartW, chartH);

  if (!candles || candles.length === 0) {
    c.fillStyle = '#3d4a5c';
    c.font = '12px "JetBrains Mono", monospace';
    c.textAlign = 'center';
    c.fillText('No candle data', plotW / 2, plotH / 2);
    blitToScreen();
    return;
  }

  // Determine visible candles (show last 80-120)
  const maxVisible = Math.min(Math.max(Math.floor(plotW / 8), 80), 120);
  const visibleCandles = candles.slice(-maxVisible);
  const numCandles = visibleCandles.length;
  if (numCandles === 0) { blitToScreen(); return; }

  // Candle geometry
  const totalCandleW = plotW / numCandles;
  const bodyW = Math.max(totalCandleW * 0.8, 3);
  const gap = totalCandleW - bodyW;

  // Price range
  let hi = -Infinity, lo = Infinity;
  let maxVol = 0;
  for (const bar of visibleCandles) {
    if (bar.h > hi) hi = bar.h;
    if (bar.l < lo) lo = bar.l;
    if ((bar.vol || 0) > maxVol) maxVol = bar.vol || 0;
  }
  const padding = (hi - lo) * 0.08 || 10;
  hi += padding;
  lo -= padding;
  const priceRange = hi - lo || 1;

  const priceY = (price) => ((hi - price) / priceRange) * (plotH * (1 - VOLUME_PCT));
  const volumeBase = plotH;
  const volumeTop = plotH * (1 - VOLUME_PCT);
  const volumeH = plotH * VOLUME_PCT;

  // Session shading (RTH 9:30-16:00 ET)
  for (let i = 0; i < numCandles; i++) {
    const bar = visibleCandles[i];
    const t = new Date(bar.time);
    if (isNaN(t)) continue;
    const etStr = t.toLocaleString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit' });
    const parts = etStr.split(':');
    const mins = parseInt(parts[0]) * 60 + parseInt(parts[1]);
    if (mins >= 570 && mins < 960) {
      const x = i * totalCandleW;
      c.fillStyle = 'rgba(77,166,255,0.03)';
      c.fillRect(x, 0, totalCandleW, plotH);
    }
  }

  // Grid lines
  c.strokeStyle = '#141c28';
  c.lineWidth = 1;
  const niceInterval = (range, targetLines) => {
    const rough = range / targetLines;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const residual = rough / mag;
    let nice;
    if (residual <= 1.5) nice = 1;
    else if (residual <= 3) nice = 2;
    else if (residual <= 7) nice = 5;
    else nice = 10;
    return nice * mag;
  };
  const priceStep = niceInterval(priceRange, 8);
  const startPrice = Math.ceil(lo / priceStep) * priceStep;
  c.font = '10px "JetBrains Mono", monospace';
  c.textAlign = 'right';
  for (let p = startPrice; p <= hi; p += priceStep) {
    const y = priceY(p);
    if (y < 0 || y > plotH) continue;
    c.beginPath();
    c.moveTo(0, Math.round(y) + 0.5);
    c.lineTo(plotW, Math.round(y) + 0.5);
    c.stroke();
    c.fillStyle = '#4a5568';
    c.fillText(fmtComma(p), chartW - 4, y + 3);
  }

  // Time axis labels
  c.textAlign = 'center';
  c.fillStyle = '#4a5568';
  c.font = '9px "JetBrains Mono", monospace';
  let lastDateStr = '';
  const labelInterval = Math.max(Math.floor(numCandles / 10), 1);
  for (let i = 0; i < numCandles; i += labelInterval) {
    const bar = visibleCandles[i];
    const t = new Date(bar.time);
    if (isNaN(t)) continue;
    const cx = i * totalCandleW + totalCandleW / 2;
    const etDate = t.toLocaleDateString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric' });
    const etTime = t.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit' });
    let label = etTime;
    if (etDate !== lastDateStr) {
      label = etDate;
      lastDateStr = etDate;
    }
    c.fillText(label, cx, plotH + 16);
    // Vertical grid
    c.beginPath();
    c.strokeStyle = '#141c28';
    c.moveTo(Math.round(cx) + 0.5, 0);
    c.lineTo(Math.round(cx) + 0.5, plotH);
    c.stroke();
  }

  // Volume bars
  if (maxVol > 0) {
    for (let i = 0; i < numCandles; i++) {
      const bar = visibleCandles[i];
      const isBull = bar.c >= bar.o;
      const vH = ((bar.vol || 0) / maxVol) * volumeH;
      const x = i * totalCandleW + gap / 2;
      c.fillStyle = isBull ? 'rgba(0,212,170,0.2)' : 'rgba(255,59,92,0.2)';
      c.fillRect(x, volumeBase - vH, bodyW, vH);
    }
  }

  // Supply/Demand zones
  const zones = detectZones(visibleCandles);
  for (const z of zones) {
    const y1 = priceY(z.high);
    const y2 = priceY(z.low);
    const x = z.startIdx * totalCandleW;
    if (z.type === 'demand') {
      c.fillStyle = 'rgba(0,212,170,0.06)';
      c.strokeStyle = 'rgba(0,212,170,0.2)';
    } else {
      c.fillStyle = 'rgba(255,59,92,0.06)';
      c.strokeStyle = 'rgba(255,59,92,0.2)';
    }
    c.fillRect(x, y1, plotW - x, y2 - y1);
    c.lineWidth = 1;
    c.strokeRect(x + 0.5, y1 + 0.5, plotW - x - 1, y2 - y1 - 1);
  }

  // Candlesticks
  for (let i = 0; i < numCandles; i++) {
    const bar = visibleCandles[i];
    const isBull = bar.c >= bar.o;
    const color = isBull ? '#00d4aa' : '#ff3b5c';
    const x = i * totalCandleW + gap / 2;
    const cx = x + bodyW / 2;

    // Wick
    const wickTop = priceY(bar.h);
    const wickBot = priceY(bar.l);
    c.strokeStyle = color;
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(Math.round(cx) + 0.5, Math.round(wickTop));
    c.lineTo(Math.round(cx) + 0.5, Math.round(wickBot));
    c.stroke();

    // Body (solid filled for both bull and bear)
    const bodyTop = priceY(Math.max(bar.o, bar.c));
    const bodyBot = priceY(Math.min(bar.o, bar.c));
    const bodyHeight = Math.max(bodyBot - bodyTop, 1);
    c.fillStyle = color;
    c.fillRect(Math.round(x), Math.round(bodyTop), Math.round(bodyW), Math.round(bodyHeight));
  }

  // Trade markers
  renderTradeMarkers(c, visibleCandles, candles.length - numCandles, totalCandleW, gap, bodyW, priceY, plotW, plotH);

  // Current price line
  if (numCandles > 0) {
    const lastBar = visibleCandles[numCandles - 1];
    const isBull = lastBar.c >= lastBar.o;
    const color = isBull ? '#00d4aa' : '#ff3b5c';
    const y = priceY(lastBar.c);
    c.setLineDash([4, 3]);
    c.strokeStyle = color;
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(0, Math.round(y) + 0.5);
    c.lineTo(plotW, Math.round(y) + 0.5);
    c.stroke();
    c.setLineDash([]);

    // Price badge on right axis
    const badgeW = PRICE_AXIS_W - 4;
    const badgeH = 18;
    const badgeX = plotW + 2;
    const badgeY = y - badgeH / 2;
    c.fillStyle = color;
    c.fillRect(badgeX, badgeY, badgeW, badgeH);
    c.fillStyle = '#fff';
    c.font = 'bold 10px "JetBrains Mono", monospace';
    c.textAlign = 'center';
    c.fillText(fmtComma(lastBar.c), badgeX + badgeW / 2, badgeY + 13);

    // Contract name
    c.fillStyle = '#4a5568';
    c.font = '8px "JetBrains Mono", monospace';
    c.textAlign = 'center';
    c.fillText('MNQH2026', badgeX + badgeW / 2, badgeY - 3);
  }

  // Crosshair
  if (S.mouse.over && S.mouse.x < plotW && S.mouse.y < plotH) {
    const mx = S.mouse.x;
    const my = S.mouse.y;
    // Snap to nearest candle
    const candleIdx = Math.min(Math.max(Math.round(mx / totalCandleW - 0.5), 0), numCandles - 1);
    const snapX = candleIdx * totalCandleW + totalCandleW / 2;

    // Crosshair lines
    c.setLineDash([4, 4]);
    c.strokeStyle = 'rgba(77,166,255,0.3)';
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(Math.round(snapX) + 0.5, 0);
    c.lineTo(Math.round(snapX) + 0.5, plotH);
    c.stroke();

    c.strokeStyle = 'rgba(77,166,255,0.4)';
    c.beginPath();
    c.moveTo(0, Math.round(my) + 0.5);
    c.lineTo(plotW, Math.round(my) + 0.5);
    c.stroke();
    c.setLineDash([]);

    // Price badge on right axis at crosshair Y
    const crossPrice = hi - (my / (plotH * (1 - VOLUME_PCT))) * priceRange;
    const cbW = PRICE_AXIS_W - 4;
    const cbH = 16;
    const cbX = plotW + 2;
    const cbY = my - cbH / 2;
    c.fillStyle = '#4da6ff';
    c.fillRect(cbX, cbY, cbW, cbH);
    c.fillStyle = '#fff';
    c.font = '10px "JetBrains Mono", monospace';
    c.textAlign = 'center';
    c.fillText(fmtComma(crossPrice), cbX + cbW / 2, cbY + 12);

    // Time badge on bottom axis
    const hoveredBar = visibleCandles[candleIdx];
    if (hoveredBar) {
      const t = new Date(hoveredBar.time);
      if (!isNaN(t)) {
        const timeLabel = t.toLocaleDateString('en-US', { timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric' })
          + ' ' + t.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit' });
        const tbW = c.measureText(timeLabel).width + 12;
        const tbH = 16;
        const tbX = snapX - tbW / 2;
        const tbY = plotH + 2;
        c.fillStyle = '#4da6ff';
        c.fillRect(tbX, tbY, tbW, tbH);
        c.fillStyle = '#fff';
        c.font = '9px "JetBrains Mono", monospace';
        c.textAlign = 'center';
        c.fillText(timeLabel, snapX, tbY + 12);
      }
    }

    // OHLCV overlay for hovered candle
    renderOHLCV(c, visibleCandles[candleIdx], numCandles > 1 ? visibleCandles[candleIdx > 0 ? candleIdx - 1 : 0] : null);
  } else {
    // OHLCV for latest candle
    if (numCandles > 0) {
      renderOHLCV(c, visibleCandles[numCandles - 1], numCandles > 1 ? visibleCandles[numCandles - 2] : null);
    }
  }

  blitToScreen();
};

const renderOHLCV = (c, bar, prevBar) => {
  if (!bar) return;
  const isBull = bar.c >= bar.o;
  const change = prevBar ? bar.c - prevBar.c : 0;
  const pct = prevBar && prevBar.c ? ((change / prevBar.c) * 100) : 0;
  const sign = change >= 0 ? '+' : '';
  const tf = S.activeTimeframe;

  // Semi-transparent background
  c.fillStyle = 'rgba(13,17,23,0.8)';
  c.fillRect(8, 4, 520, 18);

  c.font = '11px "JetBrains Mono", monospace';
  c.textAlign = 'left';
  let x = 12;
  const y = 16;

  // Symbol + TF
  c.fillStyle = '#e2e8f0';
  c.fillText('MNQ ' + tf + '  ', x, y);
  x += c.measureText('MNQ ' + tf + '  ').width;

  // O
  c.fillStyle = isBull ? '#00d4aa' : '#ff3b5c';
  c.fillText('O' + fmtComma(bar.o), x, y);
  x += c.measureText('O' + fmtComma(bar.o)).width + 6;

  // H
  c.fillStyle = '#00d4aa';
  c.fillText('H' + fmtComma(bar.h), x, y);
  x += c.measureText('H' + fmtComma(bar.h)).width + 6;

  // L
  c.fillStyle = '#ff3b5c';
  c.fillText('L' + fmtComma(bar.l), x, y);
  x += c.measureText('L' + fmtComma(bar.l)).width + 6;

  // C
  c.fillStyle = isBull ? '#00d4aa' : '#ff3b5c';
  c.fillText('C' + fmtComma(bar.c), x, y);
  x += c.measureText('C' + fmtComma(bar.c)).width + 6;

  // Change
  c.fillStyle = change >= 0 ? '#00d4aa' : '#ff3b5c';
  c.fillText(sign + change.toFixed(2) + ' (' + sign + pct.toFixed(2) + '%)', x, y);
  x += c.measureText(sign + change.toFixed(2) + ' (' + sign + pct.toFixed(2) + '%)').width + 6;

  // Volume
  c.fillStyle = '#4a5568';
  c.fillText('Vol ' + (bar.vol || 0), x, y);
};

const renderTradeMarkers = (c, visCandles, offset, totalCandleW, gap, bodyW, priceY, plotW, plotH) => {
  if (!S.trades || S.trades.length === 0) return;
  for (const trade of S.trades) {
    if (!trade.ep || !trade.entry_time) continue;
    const entryTime = new Date(trade.entry_time).getTime();
    let entryIdx = -1;
    for (let i = 0; i < visCandles.length; i++) {
      const ct = new Date(visCandles[i].time).getTime();
      if (ct >= entryTime) { entryIdx = i; break; }
    }
    if (entryIdx < 0) continue;

    const x = entryIdx * totalCandleW + totalCandleW / 2;
    const y = priceY(trade.ep);
    const isLong = (trade.dir || '').toUpperCase() === 'LONG';
    const color = isLong ? '#00d4aa' : '#ff3b5c';

    // Triangle marker
    c.fillStyle = color;
    c.beginPath();
    if (isLong) {
      c.moveTo(x, y + 5);
      c.lineTo(x - 5, y + 12);
      c.lineTo(x + 5, y + 12);
    } else {
      c.moveTo(x, y - 5);
      c.lineTo(x - 5, y - 12);
      c.lineTo(x + 5, y - 12);
    }
    c.closePath();
    c.fill();

    // PnL label
    const pnl = trade.unrealized_pnl || 0;
    const holdSec = Math.floor((Date.now() - entryTime) / 1000);
    const holdMin = Math.floor(holdSec / 60);
    const label = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(0) + ' ' + holdMin + 'm';
    c.font = 'bold 9px "JetBrains Mono", monospace';
    c.textAlign = 'center';
    c.fillStyle = color;
    c.fillText(label, x, isLong ? y + 22 : y - 16);
  }
};

const detectZones = (candles) => {
  const zones = [];
  if (candles.length < 10) return zones;
  const lookback = 5;
  for (let i = lookback; i < candles.length - lookback; i++) {
    let isSwingHigh = true, isSwingLow = true;
    for (let j = 1; j <= lookback; j++) {
      if (candles[i].h <= candles[i - j].h || candles[i].h <= candles[i + j].h) isSwingHigh = false;
      if (candles[i].l >= candles[i - j].l || candles[i].l >= candles[i + j].l) isSwingLow = false;
    }
    if (isSwingHigh) {
      zones.push({ type: 'supply', high: candles[i].h, low: Math.max(candles[i].o, candles[i].c), startIdx: i });
    }
    if (isSwingLow) {
      zones.push({ type: 'demand', high: Math.min(candles[i].o, candles[i].c), low: candles[i].l, startIdx: i });
    }
  }
  return zones.slice(-6); // Limit to last 6 zones
};

const blitToScreen = () => {
  ctx.clearRect(0, 0, chartW, chartH);
  ctx.drawImage(offscreen, 0, 0, chartW * dpr, chartH * dpr, 0, 0, chartW, chartH);
};

// ═══════════════════════════════════════════════════
// ANIMATION LOOP
// ═══════════════════════════════════════════════════
let lastFrameTime = 0;
const animate = (ts) => {
  if (S.chartDirty || (S.mouse.over && ts - lastFrameTime > 16)) {
    renderChart();
    S.chartDirty = false;
    lastFrameTime = ts;
  }
  requestAnimationFrame(animate);
};
requestAnimationFrame(animate);

// Position hold timers update
setInterval(() => {
  if (S.trades && S.trades.length > 0) {
    updatePositions();
  }
}, 1000);

// ═══════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════
fetchAllData();
setInterval(fetchAllData, 3000);
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

        if path == "/api/gamma":
            self._send_json(_get_gamma_data())
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
