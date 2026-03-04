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
<title>NQ.BOT — Paper Trading Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0e14;--panel:#10151c;--border:#1b2433;
  --green:#00d4aa;--red:#ff3b5c;--amber:#ffb800;--blue:#4da6ff;
  --text:#c8d0dc;--dim:#5a6578;--white:#e8ecf2;
}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;overflow-x:hidden}
/* HEADER */
.header{display:flex;align-items:center;padding:8px 16px;background:var(--panel);border-bottom:1px solid var(--border);gap:16px;height:42px}
.logo{font-size:16px;font-weight:700;letter-spacing:1px}
.logo .nq{color:var(--green)}.logo .bot{color:var(--dim)}
.badge{padding:2px 8px;border-radius:3px;font-size:10px;font-weight:600;letter-spacing:1px}
.badge-paper{background:rgba(255,184,0,.15);color:var(--amber);border:1px solid rgba(255,184,0,.3)}
.price-display{font-size:14px;font-weight:600;color:var(--white)}
.price-change{font-size:11px;margin-left:6px}
.price-change.up{color:var(--green)}.price-change.down{color:var(--red)}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,212,170,.4)}50%{opacity:.7;box-shadow:0 0 0 6px rgba(0,212,170,0)}}
.live-label{color:var(--green);font-size:10px;font-weight:600;letter-spacing:1px}
.clock{color:var(--dim);font-size:11px;margin-left:auto}

/* STATS ROW */
.stats-row{display:flex;gap:1px;padding:1px;background:var(--border)}
.stat-cell{flex:1;background:var(--panel);padding:8px 12px;text-align:center}
.stat-label{color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
.stat-value{font-size:14px;font-weight:600}
.stat-value.positive{color:var(--green)}.stat-value.negative{color:var(--red)}.stat-value.neutral{color:var(--white)}

/* MAIN LAYOUT */
.main{display:flex;height:calc(100vh - 42px - 52px - 250px);min-height:400px}
.chart-area{flex:1;background:var(--panel);border-right:1px solid var(--border);position:relative;overflow:hidden}
.sidebar{width:230px;background:var(--bg);overflow-y:auto;display:flex;flex-direction:column;gap:1px}

/* CHART */
#chartCanvas{width:100%;height:100%;display:block}
.ohlc-overlay{position:absolute;top:8px;left:12px;font-size:11px;color:var(--dim);z-index:10;pointer-events:none}
.ohlc-overlay .sym{color:var(--white);font-weight:600}
.ohlc-overlay .up{color:var(--green)}.ohlc-overlay .down{color:var(--red)}

/* SIDEBAR PANELS */
.side-panel{background:var(--panel);padding:10px 12px}
.side-panel-title{color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:4px}
.position-card{margin-bottom:8px;padding:6px;border:1px solid var(--border);border-radius:3px}
.pos-dir{font-weight:600;font-size:11px}.pos-dir.long{color:var(--green)}.pos-dir.short{color:var(--red)}
.pos-detail{color:var(--dim);font-size:10px;margin-top:2px}
.pos-pnl{font-weight:600;font-size:12px;margin-top:2px}
.pos-timer{color:var(--blue);font-size:10px}

/* SAFETY BARS */
.safety-item{margin-bottom:6px}
.safety-label{display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px}
.safety-bar{height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.safety-fill{height:100%;border-radius:2px;transition:width .5s}
.safety-ok{background:var(--green)}.safety-warn{background:var(--amber)}.safety-alert{background:var(--red)}
.safety-status{font-size:9px;font-weight:600}
.safety-status.ok{color:var(--green)}.safety-status.alert{color:var(--red)}.safety-status.warn{color:var(--amber)}

/* MODIFIER */
.mod-item{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;font-size:10px}
.mod-name{color:var(--dim);text-transform:uppercase}
.mod-value{font-weight:600;color:var(--white)}
.mod-reason{color:var(--dim);font-size:9px;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mod-total{border-top:1px solid var(--border);padding-top:6px;margin-top:6px;font-size:11px;font-weight:600;display:flex;justify-content:space-between}

/* DECISIONS TABLE */
.decisions-area{height:250px;background:var(--panel);border-top:1px solid var(--border);display:flex;flex-direction:column}
.decisions-header{display:flex;align-items:center;padding:8px 16px;gap:12px;border-bottom:1px solid var(--border)}
.decisions-title{color:var(--white);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px}
.decisions-count{font-size:10px;color:var(--dim)}
.decisions-scroll{flex:1;overflow-y:auto}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;background:var(--bg);color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:1px;padding:6px 12px;text-align:left;border-bottom:1px solid var(--border)}
td{padding:5px 12px;font-size:11px;border-bottom:1px solid rgba(27,36,51,.5)}
tr:nth-child(even){background:rgba(16,21,28,.5)}
tr:hover{background:rgba(77,166,255,.05)}
.badge-approved{background:rgba(0,212,170,.15);color:var(--green);padding:1px 6px;border-radius:2px;font-size:9px;font-weight:600}
.badge-rejected{background:rgba(255,59,92,.15);color:var(--red);padding:1px 6px;border-radius:2px;font-size:9px;font-weight:600}
.no-data{color:var(--dim);text-align:center;padding:40px;font-size:11px}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="logo"><span class="nq">NQ</span><span class="bot">.BOT</span></div>
  <span class="badge badge-paper">PAPER</span>
  <span class="price-display" id="lastPrice">--</span>
  <span class="price-change" id="priceChange">--</span>
  <div style="display:flex;align-items:center;gap:4px">
    <div class="live-dot"></div>
    <span class="live-label">LIVE</span>
  </div>
  <span class="clock" id="clock">--:--:-- ET</span>
</div>

<!-- STATS ROW -->
<div class="stats-row">
  <div class="stat-cell"><div class="stat-label">Session PnL</div><div class="stat-value" id="statPnl">$0.00</div></div>
  <div class="stat-cell"><div class="stat-label">Trades (W/L)</div><div class="stat-value neutral" id="statTrades">0 (0/0)</div></div>
  <div class="stat-cell"><div class="stat-label">Win Rate</div><div class="stat-value neutral" id="statWinRate">0.0%</div></div>
  <div class="stat-cell"><div class="stat-label">Profit Factor</div><div class="stat-value neutral" id="statPF">0.00</div></div>
  <div class="stat-cell"><div class="stat-label">Sharpe</div><div class="stat-value neutral" id="statSharpe">0.00</div></div>
  <div class="stat-cell"><div class="stat-label">Max DD</div><div class="stat-value neutral" id="statDD">$0.00</div></div>
  <div class="stat-cell"><div class="stat-label">Bars</div><div class="stat-value neutral" id="statBars">0</div></div>
</div>

<!-- MAIN -->
<div class="main">
  <!-- CHART -->
  <div class="chart-area">
    <div class="ohlc-overlay" id="ohlcOverlay">
      <span class="sym">MNQ 2m</span>
      <span id="ohlcO">O --</span>
      <span id="ohlcH">H --</span>
      <span id="ohlcL">L --</span>
      <span id="ohlcC">C --</span>
      <span id="ohlcChg">--</span>
    </div>
    <canvas id="chartCanvas"></canvas>
  </div>

  <!-- SIDEBAR -->
  <div class="sidebar">
    <!-- ACTIVE POSITIONS -->
    <div class="side-panel">
      <div class="side-panel-title">Active Positions</div>
      <div id="positionsContainer"><div class="no-data">No open positions</div></div>
    </div>

    <!-- SAFETY RAILS -->
    <div class="side-panel">
      <div class="side-panel-title">Safety Rails</div>
      <div id="safetyContainer">
        <div class="safety-item"><div class="safety-label"><span>Daily Loss</span><span class="safety-status ok" id="safetyDailyStatus">OK</span></div><div class="safety-bar"><div class="safety-fill safety-ok" id="safetyDailyBar" style="width:0%"></div></div><div style="font-size:9px;color:var(--dim);margin-top:1px" id="safetyDailyText">$0 / $500</div></div>
        <div class="safety-item"><div class="safety-label"><span>Consec Losses</span><span class="safety-status ok" id="safetyConsecStatus">OK</span></div><div class="safety-bar"><div class="safety-fill safety-ok" id="safetyConsecBar" style="width:0%"></div></div><div style="font-size:9px;color:var(--dim);margin-top:1px" id="safetyConsecText">0 / 5</div></div>
        <div class="safety-item"><div class="safety-label"><span>Position Size</span><span class="safety-status ok" id="safetyPosStatus">OK</span></div><div class="safety-bar"><div class="safety-fill safety-ok" id="safetyPosBar" style="width:0%"></div></div><div style="font-size:9px;color:var(--dim);margin-top:1px" id="safetyPosText">0 / 2</div></div>
        <div class="safety-item"><div class="safety-label"><span>Heartbeat</span><span class="safety-status ok" id="safetyHBStatus">OK</span></div><div class="safety-bar"><div class="safety-fill safety-ok" id="safetyHBBar" style="width:0%"></div></div><div style="font-size:9px;color:var(--dim);margin-top:1px" id="safetyHBText">0s</div></div>
      </div>
    </div>

    <!-- MODIFIERS -->
    <div class="side-panel">
      <div class="side-panel-title">Modifiers</div>
      <div id="modifiersContainer">
        <div class="mod-item"><span class="mod-name">OVERNIGHT</span><span class="mod-value" id="modON">1.00x</span></div>
        <div class="mod-item"><span class="mod-name">FOMC</span><span class="mod-value" id="modFOMC">1.00x</span></div>
        <div class="mod-item"><span class="mod-name">GAMMA</span><span class="mod-value" id="modGamma">1.00x</span></div>
        <div class="mod-item"><span class="mod-name">HAR-RV</span><span class="mod-value" id="modHARRV">1.00x</span></div>
        <div class="mod-total"><span>TOTAL</span><span id="modTotal">1.00x</span></div>
      </div>
    </div>
  </div>
</div>

<!-- DECISIONS TABLE -->
<div class="decisions-area">
  <div class="decisions-header">
    <span class="decisions-title">Trade Decisions</span>
    <span class="decisions-count" id="decisionsCount">0 approved / 0 rejected</span>
  </div>
  <div class="decisions-scroll">
    <table>
      <thead><tr><th>Time</th><th>Dir</th><th>Price</th><th>Decision</th><th>Score</th><th>Reason</th></tr></thead>
      <tbody id="decisionsBody"><tr><td colspan="6" class="no-data">No decisions yet</td></tr></tbody>
    </table>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════
let candles = [], trades = [], activeTrades = [], decisions = [];
let status = {}, safety = {}, modifiers = {};
let hoverIndex = -1;

// ═══════════════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════════════
function updateClock() {
  const now = new Date();
  const et = now.toLocaleString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  document.getElementById('clock').textContent = et + ' ET';
}
setInterval(updateClock, 1000);
updateClock();

// ═══════════════════════════════════════════════════════════
// DATA FETCHING
// ═══════════════════════════════════════════════════════════
async function fetchData() {
  try {
    const [sRes, dRes, cRes, tRes, mRes, sfRes] = await Promise.all([
      fetch('/api/status').then(r=>r.json()).catch(()=>({})),
      fetch('/api/decisions').then(r=>r.json()).catch(()=>[]),
      fetch('/api/candles').then(r=>r.json()).catch(()=>[]),
      fetch('/api/trades').then(r=>r.json()).catch(()=>[]),
      fetch('/api/modifiers').then(r=>r.json()).catch(()=>({})),
      fetch('/api/safety').then(r=>r.json()).catch(()=>({})),
    ]);
    status = sRes; decisions = dRes; candles = cRes;
    activeTrades = tRes; modifiers = mRes; safety = sfRes;
    updateUI();
  } catch(e) { console.warn('Fetch error:', e); }
}

// ═══════════════════════════════════════════════════════════
// UI UPDATES
// ═══════════════════════════════════════════════════════════
function updateUI() {
  updateHeader();
  updateStats();
  updateChart();
  updatePositions();
  updateSafety();
  updateModifiers();
  updateDecisions();
}

function fmt(n, d=2) { return (n||0).toFixed(d); }
function fmtPnl(n) { const v=n||0; return (v>=0?'+':'')+v.toFixed(2); }

function updateHeader() {
  if (!candles.length) return;
  const last = candles[candles.length-1];
  const prev = candles.length > 1 ? candles[candles.length-2] : last;
  const price = last.c;
  const chg = price - prev.c;
  const pct = prev.c ? (chg/prev.c*100) : 0;
  document.getElementById('lastPrice').textContent = fmt(price, 2);
  const ce = document.getElementById('priceChange');
  ce.textContent = `${fmtPnl(chg)} (${fmtPnl(pct)}%)`;
  ce.className = 'price-change ' + (chg >= 0 ? 'up' : 'down');
}

function updateStats() {
  const s = status;
  const pnlEl = document.getElementById('statPnl');
  const pnl = s.total_pnl || 0;
  pnlEl.textContent = '$' + fmtPnl(pnl);
  pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'positive' : 'negative');
  document.getElementById('statTrades').textContent = `${s.trade_count||0} (${s.wins||0}/${s.losses||0})`;
  document.getElementById('statWinRate').textContent = fmt(s.win_rate||0, 1) + '%';
  document.getElementById('statPF').textContent = fmt(s.profit_factor||0, 2);
  document.getElementById('statSharpe').textContent = fmt(s.sharpe_estimate||0, 2);
  const ddEl = document.getElementById('statDD');
  ddEl.textContent = '$' + fmt(s.max_drawdown||0, 2);
  ddEl.className = 'stat-value ' + ((s.max_drawdown||0) > 0 ? 'negative' : 'neutral');
  document.getElementById('statBars').textContent = candles.length;
}

// ═══════════════════════════════════════════════════════════
// CANDLESTICK CHART
// ═══════════════════════════════════════════════════════════
const chartCanvas = document.getElementById('chartCanvas');
const ctx = chartCanvas.getContext('2d');
const CHART_PAD_RIGHT = 70;
const CHART_PAD_BOTTOM = 28;
const CHART_PAD_TOP = 30;
const CHART_PAD_LEFT = 8;
const VOL_HEIGHT_RATIO = 0.15;
const MAX_VISIBLE = 80;

let mouseX = -1, mouseY = -1;

function resizeCanvas() {
  const rect = chartCanvas.parentElement.getBoundingClientRect();
  chartCanvas.width = rect.width * (window.devicePixelRatio||1);
  chartCanvas.height = rect.height * (window.devicePixelRatio||1);
  chartCanvas.style.width = rect.width + 'px';
  chartCanvas.style.height = rect.height + 'px';
  ctx.setTransform(window.devicePixelRatio||1, 0, 0, window.devicePixelRatio||1, 0, 0);
}

window.addEventListener('resize', () => { resizeCanvas(); drawChart(); });

chartCanvas.addEventListener('mousemove', (e) => {
  const rect = chartCanvas.getBoundingClientRect();
  mouseX = e.clientX - rect.left;
  mouseY = e.clientY - rect.top;
  drawChart();
});
chartCanvas.addEventListener('mouseleave', () => {
  mouseX = -1; mouseY = -1; hoverIndex = -1;
  drawChart();
  updateOHLC(-1);
});

function updateChart() {
  resizeCanvas();
  drawChart();
}

function drawChart() {
  const W = chartCanvas.width / (window.devicePixelRatio||1);
  const H = chartCanvas.height / (window.devicePixelRatio||1);
  ctx.clearRect(0, 0, W, H);

  if (!candles.length) {
    ctx.fillStyle = '#5a6578';
    ctx.font = '12px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    ctx.fillText('Waiting for candle data...', W/2, H/2);
    return;
  }

  const visible = candles.slice(-MAX_VISIBLE);
  const n = visible.length;
  const chartW = W - CHART_PAD_LEFT - CHART_PAD_RIGHT;
  const chartH = H - CHART_PAD_TOP - CHART_PAD_BOTTOM;
  const volH = chartH * VOL_HEIGHT_RATIO;
  const priceH = chartH - volH - 4;
  const candleW = chartW / n;
  const bodyW = Math.max(1, candleW * 0.65);

  // Price range
  let hi = -Infinity, lo = Infinity, maxVol = 0;
  for (const c of visible) {
    if (c.h > hi) hi = c.h;
    if (c.l < lo) lo = c.l;
    if ((c.vol||0) > maxVol) maxVol = c.vol||0;
  }
  const pad = (hi - lo) * 0.08 || 5;
  hi += pad; lo -= pad;
  const priceRange = hi - lo || 1;

  function priceY(p) { return CHART_PAD_TOP + (1 - (p - lo) / priceRange) * priceH; }
  function candleX(i) { return CHART_PAD_LEFT + i * candleW + candleW / 2; }

  // Grid lines
  ctx.strokeStyle = '#1b2433';
  ctx.lineWidth = 0.5;
  const nGrid = 5;
  for (let i = 0; i <= nGrid; i++) {
    const y = CHART_PAD_TOP + (i / nGrid) * priceH;
    ctx.beginPath(); ctx.moveTo(CHART_PAD_LEFT, y); ctx.lineTo(W - CHART_PAD_RIGHT, y); ctx.stroke();
    const price = hi - (i / nGrid) * priceRange;
    ctx.fillStyle = '#5a6578';
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.textAlign = 'left';
    ctx.fillText(price.toFixed(2), W - CHART_PAD_RIGHT + 6, y + 3);
  }

  // Supply/Demand zones
  drawZones(visible, candleX, priceY, n, chartW);

  // Volume bars
  const volBase = CHART_PAD_TOP + priceH + 4 + volH;
  for (let i = 0; i < n; i++) {
    const c = visible[i];
    const vH = maxVol > 0 ? ((c.vol||0) / maxVol) * volH : 0;
    const isUp = c.c >= c.o;
    ctx.fillStyle = isUp ? 'rgba(0,212,170,0.25)' : 'rgba(255,59,92,0.25)';
    ctx.fillRect(candleX(i) - bodyW/2, volBase - vH, bodyW, vH);
  }

  // Candles
  for (let i = 0; i < n; i++) {
    const c = visible[i];
    const isUp = c.c >= c.o;
    const x = candleX(i);
    const oY = priceY(c.o), cY = priceY(c.c);
    const hY = priceY(c.h), lY = priceY(c.l);

    // Wick
    ctx.strokeStyle = isUp ? '#00d4aa' : '#ff3b5c';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, hY); ctx.lineTo(x, lY); ctx.stroke();

    // Body
    const top = Math.min(oY, cY);
    const bodyH = Math.max(1, Math.abs(oY - cY));
    if (isUp) {
      // Hollow green body
      ctx.strokeStyle = '#00d4aa';
      ctx.lineWidth = 1;
      ctx.strokeRect(x - bodyW/2, top, bodyW, bodyH);
    } else {
      // Filled red body
      ctx.fillStyle = '#ff3b5c';
      ctx.fillRect(x - bodyW/2, top, bodyW, bodyH);
    }
  }

  // Trade markers
  drawTradeMarkers(visible, candleX, priceY, n);

  // Current price line
  if (visible.length) {
    const lastC = visible[visible.length - 1];
    const cpY = priceY(lastC.c);
    const isUp = lastC.c >= lastC.o;
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = isUp ? '#00d4aa' : '#ff3b5c';
    ctx.lineWidth = 0.8;
    ctx.beginPath(); ctx.moveTo(CHART_PAD_LEFT, cpY); ctx.lineTo(W - CHART_PAD_RIGHT, cpY); ctx.stroke();
    ctx.setLineDash([]);
    // Price badge
    const badgeColor = isUp ? '#00d4aa' : '#ff3b5c';
    ctx.fillStyle = badgeColor;
    ctx.fillRect(W - CHART_PAD_RIGHT, cpY - 8, CHART_PAD_RIGHT - 2, 16);
    ctx.fillStyle = '#0a0e14';
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(lastC.c.toFixed(2), W - CHART_PAD_RIGHT/2, cpY + 3);
  }

  // Time axis
  if (mouseX < 0) {
    ctx.fillStyle = '#5a6578';
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    const step = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += step) {
      const c = visible[i];
      const t = new Date(c.time);
      const label = t.toLocaleString('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', hour12:false});
      ctx.fillText(label, candleX(i), H - 6);
    }
  }

  // Crosshair
  if (mouseX >= CHART_PAD_LEFT && mouseX <= W - CHART_PAD_RIGHT && mouseY >= CHART_PAD_TOP && mouseY <= CHART_PAD_TOP + priceH) {
    // Find hovered candle
    const ci = Math.floor((mouseX - CHART_PAD_LEFT) / candleW);
    hoverIndex = Math.max(0, Math.min(ci, n - 1));
    const hc = visible[hoverIndex];

    // Vertical line
    const cx = candleX(hoverIndex);
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(200,208,220,0.3)';
    ctx.lineWidth = 0.5;
    ctx.beginPath(); ctx.moveTo(cx, CHART_PAD_TOP); ctx.lineTo(cx, CHART_PAD_TOP + priceH); ctx.stroke();

    // Horizontal line
    ctx.beginPath(); ctx.moveTo(CHART_PAD_LEFT, mouseY); ctx.lineTo(W - CHART_PAD_RIGHT, mouseY); ctx.stroke();
    ctx.setLineDash([]);

    // Price badge on Y axis
    const hoverPrice = hi - ((mouseY - CHART_PAD_TOP) / priceH) * priceRange;
    ctx.fillStyle = '#2a3545';
    ctx.fillRect(W - CHART_PAD_RIGHT, mouseY - 8, CHART_PAD_RIGHT - 2, 16);
    ctx.fillStyle = '#e8ecf2';
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(hoverPrice.toFixed(2), W - CHART_PAD_RIGHT/2, mouseY + 3);

    // Time badge at bottom
    const t = new Date(hc.time);
    const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const timeStr = `${days[t.getDay()]} ${months[t.getMonth()]} ${String(t.getDate()).padStart(2,'0')} ${String(t.getHours()).padStart(2,'0')}:${String(t.getMinutes()).padStart(2,'0')}`;
    const tw = ctx.measureText(timeStr).width + 12;
    ctx.fillStyle = '#2a3545';
    ctx.fillRect(cx - tw/2, H - CHART_PAD_BOTTOM, tw, 18);
    ctx.fillStyle = '#e8ecf2';
    ctx.textAlign = 'center';
    ctx.fillText(timeStr, cx, H - CHART_PAD_BOTTOM + 12);

    updateOHLC(hoverIndex, visible);
  } else {
    updateOHLC(-1);
  }
}

function updateOHLC(idx, visible) {
  const data = (idx >= 0 && visible) ? visible[idx] : (candles.length ? candles[candles.length-1] : null);
  if (!data) return;
  const prev = candles.length > 1 ? candles[candles.length-2] : data;
  const chg = data.c - data.o;
  const pct = data.o ? (chg/data.o*100) : 0;
  const cls = chg >= 0 ? 'up' : 'down';
  document.getElementById('ohlcO').innerHTML = `O <span class="${cls}">${fmt(data.o)}</span>`;
  document.getElementById('ohlcH').innerHTML = `H <span class="${cls}">${fmt(data.h)}</span>`;
  document.getElementById('ohlcL').innerHTML = `L <span class="${cls}">${fmt(data.l)}</span>`;
  document.getElementById('ohlcC').innerHTML = `C <span class="${cls}">${fmt(data.c)}</span>`;
  document.getElementById('ohlcChg').innerHTML = `<span class="${cls}">${fmtPnl(chg)} (${fmtPnl(pct)}%)</span>`;
}

// ═══════════════════════════════════════════════════════════
// SUPPLY/DEMAND ZONES
// ═══════════════════════════════════════════════════════════
function detectZones(visible) {
  const zones = [];
  if (visible.length < 5) return zones;
  for (let i = 2; i < visible.length - 2; i++) {
    const c = visible[i];
    // Swing high -> supply zone
    if (c.h > visible[i-1].h && c.h > visible[i-2].h && c.h > visible[i+1].h && c.h > visible[i+2].h) {
      zones.push({type:'supply', top:c.h, bottom:Math.max(c.o, c.c), startIdx:i-1, endIdx:Math.min(i+2, visible.length-1)});
    }
    // Swing low -> demand zone
    if (c.l < visible[i-1].l && c.l < visible[i-2].l && c.l < visible[i+1].l && c.l < visible[i+2].l) {
      zones.push({type:'demand', top:Math.min(c.o, c.c), bottom:c.l, startIdx:i-1, endIdx:Math.min(i+2, visible.length-1)});
    }
  }
  return zones;
}

function drawZones(visible, candleX, priceY, n, chartW) {
  const zones = detectZones(visible);
  for (const z of zones) {
    const x1 = candleX(z.startIdx) - (chartW/n)/2;
    const x2 = candleX(n-1) + (chartW/n)/2;  // extend to right
    const y1 = priceY(z.top);
    const y2 = priceY(z.bottom);
    if (z.type === 'supply') {
      ctx.fillStyle = 'rgba(255,59,92,0.06)';
      ctx.strokeStyle = 'rgba(255,59,92,0.15)';
    } else {
      ctx.fillStyle = 'rgba(0,212,170,0.06)';
      ctx.strokeStyle = 'rgba(0,212,170,0.15)';
    }
    ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
    ctx.lineWidth = 0.5;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  }
}

// ═══════════════════════════════════════════════════════════
// TRADE MARKERS
// ═══════════════════════════════════════════════════════════
function drawTradeMarkers(visible, candleX, priceY, n) {
  if (!Array.isArray(activeTrades)) return;

  // Match trades to visible candles by time
  for (const trade of activeTrades) {
    if (!trade.ep || !trade.entry_time) continue;

    // Find entry candle index
    let entryIdx = -1;
    const entryTime = new Date(trade.entry_time).getTime();
    for (let i = 0; i < n; i++) {
      const ct = new Date(visible[i].time).getTime();
      if (Math.abs(ct - entryTime) < 130000) { entryIdx = i; break; } // within ~2min
    }
    if (entryIdx < 0) continue;

    const x = candleX(entryIdx);
    const y = priceY(trade.ep);
    const isLong = (trade.dir || '').toLowerCase() === 'long';

    // Arrow marker
    ctx.fillStyle = isLong ? '#00d4aa' : '#ff3b5c';
    ctx.beginPath();
    if (isLong) {
      ctx.moveTo(x, y); ctx.lineTo(x-5, y+10); ctx.lineTo(x+5, y+10);
    } else {
      ctx.moveTo(x, y); ctx.lineTo(x-5, y-10); ctx.lineTo(x+5, y-10);
    }
    ctx.fill();

    // If trade has exit info
    if (trade.exit_price && trade.exit_time) {
      let exitIdx = -1;
      const exitTime = new Date(trade.exit_time).getTime();
      for (let i = 0; i < n; i++) {
        const ct = new Date(visible[i].time).getTime();
        if (Math.abs(ct - exitTime) < 130000) { exitIdx = i; break; }
      }
      if (exitIdx >= 0) {
        const ex = candleX(exitIdx);
        const ey = priceY(trade.exit_price);

        // Dashed line
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = (trade.unrealized_pnl||0) >= 0 ? '#00d4aa' : '#ff3b5c';
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(ex, ey); ctx.stroke();
        ctx.setLineDash([]);

        // Exit dot
        ctx.fillStyle = '#e8ecf2';
        ctx.beginPath(); ctx.arc(ex, ey, 3, 0, Math.PI*2); ctx.fill();

        // PnL label
        const pnl = trade.unrealized_pnl || 0;
        const holdMs = exitTime - entryTime;
        const holdMin = Math.round(holdMs / 60000);
        const label = `${pnl>=0?'+':''}$${pnl.toFixed(0)} · ${holdMin}m`;
        const mx = (x + ex) / 2;
        const my = (y + ey) / 2 - 8;
        ctx.fillStyle = pnl >= 0 ? '#00d4aa' : '#ff3b5c';
        ctx.font = '9px JetBrains Mono, monospace';
        ctx.textAlign = 'center';
        ctx.fillText(label, mx, my);
      }
    }
  }
}

// ═══════════════════════════════════════════════════════════
// POSITIONS PANEL
// ═══════════════════════════════════════════════════════════
function updatePositions() {
  const el = document.getElementById('positionsContainer');
  if (!Array.isArray(activeTrades) || !activeTrades.length) {
    el.innerHTML = '<div class="no-data">No open positions</div>';
    return;
  }
  el.innerHTML = activeTrades.map(t => {
    const isLong = (t.dir||'').toLowerCase() === 'long';
    const pnl = t.unrealized_pnl || 0;
    const pnlCls = pnl >= 0 ? 'positive' : 'negative';
    const holdSec = t.entry_time ? Math.floor((Date.now() - new Date(t.entry_time).getTime()) / 1000) : 0;
    const mm = String(Math.floor(holdSec/60)).padStart(2,'0');
    const ss = String(holdSec%60).padStart(2,'0');
    return `<div class="position-card">
      <div class="pos-dir ${isLong?'long':'short'}">${(t.dir||'?').toUpperCase()} × ${t.contracts||1}</div>
      <div class="pos-detail">Entry: ${fmt(t.ep)} | Mod: ${fmt(t.modifier||1,2)}x</div>
      <div class="pos-pnl" style="color:var(--${pnl>=0?'green':'red'})">${fmtPnl(pnl)}</div>
      <div class="pos-timer">${mm}:${ss}</div>
    </div>`;
  }).join('');
}
// Refresh hold timers every second
setInterval(updatePositions, 1000);

// ═══════════════════════════════════════════════════════════
// SAFETY RAILS
// ═══════════════════════════════════════════════════════════
function updateSafety() {
  const s = safety;
  setSafety('Daily', Math.abs(s.daily_pnl||0), s.daily_limit||500, `$${fmt(Math.abs(s.daily_pnl||0))} / $${s.daily_limit||500}`);
  setSafety('Consec', s.consec_losses||0, s.max_consec||5, `${s.consec_losses||0} / ${s.max_consec||5}`);
  setSafety('Pos', s.position_size||0, s.max_position||2, `${s.position_size||0} / ${s.max_position||2}`);
  const hbAge = s.heartbeat_age_sec || 0;
  setSafety('HB', Math.min(hbAge, 300), 300, `${fmt(hbAge, 0)}s`);
}

function setSafety(name, value, max, text) {
  const pct = max > 0 ? Math.min(100, (value/max)*100) : 0;
  const bar = document.getElementById('safety'+name+'Bar');
  const statusEl = document.getElementById('safety'+name+'Status');
  const textEl = document.getElementById('safety'+name+'Text');
  bar.style.width = pct + '%';
  bar.className = 'safety-fill ' + (pct < 60 ? 'safety-ok' : pct < 85 ? 'safety-warn' : 'safety-alert');
  statusEl.textContent = pct < 85 ? 'OK' : 'ALERT';
  statusEl.className = 'safety-status ' + (pct < 60 ? 'ok' : pct < 85 ? 'warn' : 'alert');
  if (textEl) textEl.textContent = text;
}

// ═══════════════════════════════════════════════════════════
// MODIFIERS
// ═══════════════════════════════════════════════════════════
function updateModifiers() {
  const m = modifiers;
  const setMod = (id, obj) => {
    const el = document.getElementById(id);
    if (el) el.textContent = fmt(obj && obj.value !== undefined ? obj.value : 1.0, 2) + 'x';
  };
  setMod('modON', m.overnight);
  setMod('modFOMC', m.fomc);
  setMod('modGamma', m.gamma);
  setMod('modHARRV', m.har_rv);
  const totalEl = document.getElementById('modTotal');
  if (totalEl) totalEl.textContent = fmt(m.total || 1.0, 2) + 'x';
}

// ═══════════════════════════════════════════════════════════
// DECISIONS TABLE
// ═══════════════════════════════════════════════════════════
function updateDecisions() {
  const body = document.getElementById('decisionsBody');
  const countEl = document.getElementById('decisionsCount');
  if (!Array.isArray(decisions) || !decisions.length) {
    body.innerHTML = '<tr><td colspan="6" class="no-data">No decisions yet</td></tr>';
    countEl.textContent = '0 approved / 0 rejected';
    return;
  }

  const approved = decisions.filter(d => d.decision === 'APPROVED').length;
  const rejected = decisions.filter(d => d.decision === 'REJECTED').length;
  countEl.textContent = `${approved} approved / ${rejected} rejected`;

  // Show most recent first
  const sorted = [...decisions].reverse();
  body.innerHTML = sorted.map(d => {
    const t = new Date(d.timestamp);
    const time = t.toLocaleString('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
    const isApproved = d.decision === 'APPROVED';
    const badge = isApproved ? '<span class="badge-approved">APPROVED</span>' : '<span class="badge-rejected">REJECTED</span>';
    const score = d.confluence_score != null ? fmt(d.confluence_score, 2) : '--';
    const reason = d.rejection_stage || (isApproved ? 'Signal approved' : '--');
    return `<tr>
      <td style="color:var(--dim)">${time}</td>
      <td style="color:${d.signal_direction==='LONG'?'var(--green)':'var(--red)'}">${d.signal_direction||'--'}</td>
      <td>${fmt(d.price_at_signal||0)}</td>
      <td>${badge}</td>
      <td>${score}</td>
      <td style="color:var(--dim);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${reason}</td>
    </tr>`;
  }).join('');
}

// ═══════════════════════════════════════════════════════════
// AUTO-REFRESH
// ═══════════════════════════════════════════════════════════
fetchData();
setInterval(fetchData, 3000);
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
        path = self.path.split("?")[0]

        if path == "/":
            self._send_html(DASHBOARD_HTML)
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
