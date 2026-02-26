import { useState, useEffect, useRef, useCallback, useMemo } from "react";

// ═══════════════════════════════════════════════════════════════
// NQ FORENSIC TRADE VISUALIZER — Institutional-Grade Chart System
// Canvas-based OHLCV + Trade Overlay + Session Logic + Analytics
// ═══════════════════════════════════════════════════════════════

// ── THEME ──────────────────────────────────────────────────────
const T = {
  bg: "#080b12", bgPanel: "#0c1018", bgCard: "#111620",
  bgHover: "#161d2a", bgActive: "#1a2332",
  border: "#1a2030", borderLight: "#232d40",
  grid: "#141c28", gridMajor: "#1a2435",
  text: "#8892a4", textMuted: "#556178", textBright: "#c8d0e0",
  textWhite: "#e8ecf4",
  green: "#00c087", greenDim: "#00c08730", greenBg: "#00c08712",
  red: "#e5334b", redDim: "#e5334b30", redBg: "#e5334b12",
  blue: "#3b82f6", blueDim: "#3b82f620",
  amber: "#f59e0b", amberDim: "#f59e0b20",
  purple: "#a855f7", purpleDim: "#a855f720",
  cyan: "#06b6d4",
  maintenanceShade: "#f59e0b08", maintenanceBorder: "#f59e0b15",
  crosshair: "#3b82f650",
  selection: "#3b82f620",
};

// ── SYNTHETIC DATA GENERATION ─────────────────────────────────
// Matches your NQ HC filter profile: 62 trades, PF~2.35, etc.
function generateMarketData(startDate, days, intervalMin = 5) {
  const candles = [];
  let price = 21500;
  const barsPerDay = Math.floor((23 * 60) / intervalMin); // NQ trades ~23hrs
  const d = new Date(startDate);

  for (let day = 0; day < days; day++) {
    const dayOfWeek = d.getDay();
    if (dayOfWeek === 0 || dayOfWeek === 6) { d.setDate(d.getDate() + 1); continue; }

    const dayStart = new Date(d);
    dayStart.setHours(18, 0, 0, 0); // NQ opens 6PM ET prior day

    const trendBias = (Math.random() - 0.48) * 0.3;
    const volatility = 8 + Math.random() * 12;

    for (let bar = 0; bar < barsPerDay; bar++) {
      const ts = new Date(dayStart.getTime() + bar * intervalMin * 60000);
      const hour = ts.getHours();
      const min = ts.getMinutes();

      // Skip maintenance window 4:30-6:00 PM ET
      if ((hour === 16 && min >= 30) || hour === 17 || (hour === 18 && min === 0)) continue;

      const sessionVol = (hour >= 9 && hour <= 11) ? 1.8 : (hour >= 14 && hour <= 16) ? 1.4 : 0.7;
      const move = (Math.random() - 0.5 + trendBias) * volatility * sessionVol;
      const o = price;
      const h = o + Math.abs(move) + Math.random() * volatility * 0.5;
      const l = o - Math.abs(move) - Math.random() * volatility * 0.5;
      const c = o + move;
      const v = Math.floor((500 + Math.random() * 2000) * sessionVol);

      candles.push({
        time: ts.getTime(),
        open: Math.round(o * 100) / 100,
        high: Math.round(Math.max(o, c, h) * 100) / 100,
        low: Math.round(Math.min(o, c, l) * 100) / 100,
        close: Math.round(c * 100) / 100,
        volume: v,
      });
      price = c;
    }
    d.setDate(d.getDate() + 1);
  }
  return candles;
}

function generateTrades(candles, count = 62) {
  const trades = [];
  const regimes = ["trending_up", "trending_down", "ranging", "unknown"];
  const regimeWeights = [0.34, 0.19, 0.16, 0.31];
  const htfBiases = ["long", "short", "neutral"];
  const exitTypes = ["pt1_partial", "trailing_stop", "stop_loss", "pt2_target", "be_plus"];
  const exitWeights = [0.25, 0.30, 0.20, 0.15, 0.10];

  const rthCandles = candles.filter(c => {
    const h = new Date(c.time).getHours();
    return h >= 9 && h <= 15;
  });

  const step = Math.floor(rthCandles.length / (count + 5));

  for (let i = 0; i < count; i++) {
    const idx = 20 + i * step + Math.floor(Math.random() * Math.min(step * 0.5, 10));
    if (idx >= rthCandles.length - 30) break;

    const entryCandle = rthCandles[idx];
    const side = Math.random() > 0.48 ? "long" : "short";
    const entryPrice = entryCandle.close;
    const signalScore = 0.75 + Math.random() * 0.23;
    const stopDist = 12 + Math.random() * 18; // 12-30pts, HC compliant
    const tp1Dist = stopDist * 1.5;
    const tp2Dist = stopDist * 3;

    const stopPrice = side === "long" ? entryPrice - stopDist : entryPrice + stopDist;
    const tp1Price = side === "long" ? entryPrice + tp1Dist : entryPrice - tp1Dist;
    const tp2Price = side === "long" ? entryPrice + tp2Dist : entryPrice - tp2Dist;

    // Pick regime
    let r = Math.random(), cumW = 0, regime = regimes[3];
    for (let ri = 0; ri < regimes.length; ri++) {
      cumW += regimeWeights[ri]; if (r < cumW) { regime = regimes[ri]; break; }
    }

    // Pick exit type
    r = Math.random(); cumW = 0; let exitType = exitTypes[0];
    for (let ei = 0; ei < exitTypes.length; ei++) {
      cumW += exitWeights[ei]; if (r < cumW) { exitType = exitTypes[ei]; break; }
    }

    const isWin = exitType !== "stop_loss";
    const holdBars = 5 + Math.floor(Math.random() * 40);
    const exitIdx = Math.min(idx + holdBars, rthCandles.length - 1);
    const exitCandle = rthCandles[exitIdx];

    let exitPrice, pnl, c1Pnl, c2Pnl;
    const ptPerDollar = 2; // MNQ $2/point

    if (exitType === "stop_loss") {
      exitPrice = stopPrice + (Math.random() - 0.5) * 2;
      pnl = side === "long" ? (exitPrice - entryPrice) * ptPerDollar * 2 : (entryPrice - exitPrice) * ptPerDollar * 2;
      c1Pnl = pnl / 2; c2Pnl = pnl / 2;
    } else if (exitType === "pt1_partial") {
      const c1Exit = tp1Price + (Math.random() - 0.5) * 1;
      const c2Exit = entryPrice + (side === "long" ? 1 : -1) * stopDist * (0.5 + Math.random() * 1.5);
      c1Pnl = (side === "long" ? c1Exit - entryPrice : entryPrice - c1Exit) * ptPerDollar;
      c2Pnl = (side === "long" ? c2Exit - entryPrice : entryPrice - c2Exit) * ptPerDollar;
      exitPrice = c2Exit; pnl = c1Pnl + c2Pnl;
    } else if (exitType === "pt2_target") {
      const c1Exit = tp1Price;
      c1Pnl = (side === "long" ? c1Exit - entryPrice : entryPrice - c1Exit) * ptPerDollar;
      exitPrice = tp2Price + (Math.random() - 0.5) * 3;
      c2Pnl = (side === "long" ? exitPrice - entryPrice : entryPrice - exitPrice) * ptPerDollar;
      pnl = c1Pnl + c2Pnl;
    } else if (exitType === "trailing_stop") {
      const mfe = stopDist * (1.5 + Math.random() * 2);
      exitPrice = side === "long" ? entryPrice + mfe * 0.6 : entryPrice - mfe * 0.6;
      c1Pnl = tp1Dist * ptPerDollar;
      c2Pnl = (side === "long" ? exitPrice - entryPrice : entryPrice - exitPrice) * ptPerDollar;
      pnl = c1Pnl + c2Pnl;
    } else {
      exitPrice = entryPrice + (side === "long" ? 2 : -2);
      c1Pnl = 2 * ptPerDollar; c2Pnl = 2 * ptPerDollar;
      pnl = c1Pnl + c2Pnl;
    }

    const slippage = Math.round((0.25 + Math.random() * 1.5) * 100) / 100;
    pnl = Math.round((pnl - slippage) * 100) / 100;

    const mfe = Math.abs(stopDist * (0.5 + Math.random() * 3));
    const mae = Math.abs(stopDist * (0.1 + Math.random() * 0.8));

    trades.push({
      id: i + 1,
      entryTime: entryCandle.time,
      exitTime: exitCandle.time,
      side,
      qty: 2,
      entryPrice: Math.round(entryPrice * 100) / 100,
      exitPrice: Math.round(exitPrice * 100) / 100,
      stopPrice: Math.round(stopPrice * 100) / 100,
      tp1Price: Math.round(tp1Price * 100) / 100,
      tp2Price: Math.round(tp2Price * 100) / 100,
      stopDist: Math.round(stopDist * 100) / 100,
      signalScore: Math.round(signalScore * 1000) / 1000,
      regime,
      htfBias: htfBiases[Math.floor(Math.random() * 3)],
      exitType,
      pnl,
      c1Pnl: Math.round(c1Pnl * 100) / 100,
      c2Pnl: Math.round(c2Pnl * 100) / 100,
      slippage,
      rMultiple: Math.round((pnl / (stopDist * ptPerDollar * 2)) * 100) / 100,
      mfe: Math.round(mfe * 100) / 100,
      mae: Math.round(mae * 100) / 100,
      holdBars,
      isCompliant: true,
    });
  }
  return trades;
}

// ── UTILITIES ─────────────────────────────────────────────────
function formatPrice(p) { return p.toFixed(2); }
function formatTime(ts) {
  const d = new Date(ts);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
}
function formatDate(ts) {
  const d = new Date(ts);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
function formatFull(ts) {
  const d = new Date(ts);
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} ${formatTime(ts)}`;
}
function isMaintenance(ts) {
  const d = new Date(ts);
  const h = d.getHours(), m = d.getMinutes();
  return (h === 16 && m >= 30) || h === 17 || (h === 18 && m === 0);
}

// ── MAIN COMPONENT ────────────────────────────────────────────
export default function NQForensicVisualizer() {
  // Data
  const [candles, setCandles] = useState([]);
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);

  // View state
  const [viewStart, setViewStart] = useState(0);
  const [viewEnd, setViewEnd] = useState(200);
  const [selectedTrade, setSelectedTrade] = useState(null);
  const [crosshairPos, setCrosshairPos] = useState(null);
  const [hoveredCandle, setHoveredCandle] = useState(null);
  const [timeframe, setTimeframe] = useState("5m");

  // Filters
  const [filterRegime, setFilterRegime] = useState("all");
  const [filterSide, setFilterSide] = useState("all");
  const [filterExit, setFilterExit] = useState("all");
  const [filterMinScore, setFilterMinScore] = useState(0.75);

  // Refs
  const chartRef = useRef(null);
  const volRef = useRef(null);
  const containerRef = useRef(null);
  const isDragging = useRef(false);
  const dragStart = useRef(0);
  const dragViewStart = useRef(0);

  // Chart dimensions
  const CHART_H = 480;
  const VOL_H = 80;
  const PRICE_AXIS_W = 85;
  const TIME_AXIS_H = 28;
  const MIN_CANDLE_W = 3;
  const MAX_CANDLE_W = 24;

  // Init data
  useEffect(() => {
    const c = generateMarketData("2026-01-06", 40, 5);
    const t = generateTrades(c, 62);
    setCandles(c);
    setTrades(t);
    setViewStart(Math.max(0, c.length - 250));
    setViewEnd(c.length);
    setLoading(false);
  }, []);

  // Filtered trades
  const filteredTrades = useMemo(() => {
    return trades.filter(t => {
      if (filterRegime !== "all" && t.regime !== filterRegime) return false;
      if (filterSide !== "all" && t.side !== filterSide) return false;
      if (filterExit !== "all" && t.exitType !== filterExit) return false;
      if (t.signalScore < filterMinScore) return false;
      return true;
    });
  }, [trades, filterRegime, filterSide, filterExit, filterMinScore]);

  // Visible candles
  const visibleCandles = useMemo(() => {
    return candles.slice(viewStart, viewEnd);
  }, [candles, viewStart, viewEnd]);

  // Price range
  const priceRange = useMemo(() => {
    if (visibleCandles.length === 0) return { min: 0, max: 1 };
    let min = Infinity, max = -Infinity;
    visibleCandles.forEach(c => { min = Math.min(min, c.low); max = Math.max(max, c.high); });

    // Include trade levels if a trade is selected
    if (selectedTrade) {
      min = Math.min(min, selectedTrade.stopPrice, selectedTrade.entryPrice);
      max = Math.max(max, selectedTrade.tp2Price, selectedTrade.entryPrice);
    }

    const pad = (max - min) * 0.08;
    return { min: min - pad, max: max + pad };
  }, [visibleCandles, selectedTrade]);

  // Volume range
  const volRange = useMemo(() => {
    if (visibleCandles.length === 0) return { max: 1 };
    return { max: Math.max(...visibleCandles.map(c => c.volume)) };
  }, [visibleCandles]);

  // Get chart width
  const getChartWidth = useCallback(() => {
    if (!containerRef.current) return 800;
    return containerRef.current.offsetWidth - PRICE_AXIS_W;
  }, []);

  // Coordinate transforms
  const priceToY = useCallback((price) => {
    const { min, max } = priceRange;
    return CHART_H - ((price - min) / (max - min)) * CHART_H;
  }, [priceRange]);

  const indexToX = useCallback((idx) => {
    const w = getChartWidth();
    const count = viewEnd - viewStart;
    if (count === 0) return 0;
    return ((idx - viewStart) / count) * w;
  }, [viewStart, viewEnd, getChartWidth]);

  const xToIndex = useCallback((x) => {
    const w = getChartWidth();
    const count = viewEnd - viewStart;
    return Math.floor((x / w) * count) + viewStart;
  }, [viewStart, viewEnd, getChartWidth]);

  // ── RENDER CHART ──────────────────────────────────────────
  useEffect(() => {
    const canvas = chartRef.current;
    const volCanvas = volRef.current;
    if (!canvas || !volCanvas || visibleCandles.length === 0) return;

    const w = getChartWidth() + PRICE_AXIS_W;
    const dpr = window.devicePixelRatio || 1;

    // Setup main canvas
    canvas.width = w * dpr;
    canvas.height = CHART_H * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = CHART_H + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    // Setup volume canvas
    volCanvas.width = w * dpr;
    volCanvas.height = VOL_H * dpr;
    volCanvas.style.width = w + "px";
    volCanvas.style.height = VOL_H + "px";
    const vctx = volCanvas.getContext("2d");
    vctx.scale(dpr, dpr);

    const chartW = w - PRICE_AXIS_W;
    const count = visibleCandles.length;
    const candleW = Math.max(MIN_CANDLE_W, Math.min(MAX_CANDLE_W, chartW / count - 1));
    const bodyW = Math.max(1, candleW - 2);

    // ── Background
    ctx.fillStyle = T.bg;
    ctx.fillRect(0, 0, w, CHART_H);
    vctx.fillStyle = T.bg;
    vctx.fillRect(0, 0, w, VOL_H);

    // ── Grid lines (horizontal)
    const { min: pMin, max: pMax } = priceRange;
    const pRange = pMax - pMin;
    const gridStep = pRange > 200 ? 50 : pRange > 100 ? 25 : pRange > 50 ? 10 : 5;
    const gridStart = Math.ceil(pMin / gridStep) * gridStep;

    ctx.strokeStyle = T.grid;
    ctx.lineWidth = 0.5;
    ctx.setLineDash([]);

    for (let p = gridStart; p <= pMax; p += gridStep) {
      const y = priceToY(p);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      ctx.stroke();

      // Price labels on axis
      ctx.fillStyle = T.textMuted;
      ctx.font = "11px 'SF Mono', 'Cascadia Code', 'JetBrains Mono', monospace";
      ctx.textAlign = "right";
      ctx.fillText(formatPrice(p), w - 8, y + 4);
    }

    // ── Maintenance window shading
    visibleCandles.forEach((c, i) => {
      if (isMaintenance(c.time)) {
        const x = (i / count) * chartW;
        ctx.fillStyle = T.maintenanceShade;
        ctx.fillRect(x, 0, candleW + 1, CHART_H);
      }
    });

    // ── Day separators
    let lastDay = -1;
    visibleCandles.forEach((c, i) => {
      const d = new Date(c.time);
      const day = d.getDate();
      if (day !== lastDay && lastDay !== -1) {
        const x = (i / count) * chartW;
        ctx.strokeStyle = T.gridMajor;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, CHART_H);
        ctx.stroke();

        ctx.fillStyle = T.textMuted;
        ctx.font = "10px 'SF Mono', monospace";
        ctx.textAlign = "center";
        ctx.fillText(formatDate(c.time), x, CHART_H - 4);
      }
      lastDay = day;
    });

    // ── SELECTED TRADE OVERLAY (background layers)
    if (selectedTrade) {
      const st = selectedTrade;
      const entryIdx = candles.findIndex(c => c.time >= st.entryTime);
      const exitIdx = candles.findIndex(c => c.time >= st.exitTime);

      if (entryIdx >= viewStart && entryIdx < viewEnd) {
        const eX = ((entryIdx - viewStart) / count) * chartW;
        const xX = exitIdx >= viewStart ? ((exitIdx - viewStart) / count) * chartW : chartW;

        // Stop level line
        const stopY = priceToY(st.stopPrice);
        ctx.strokeStyle = T.red;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(eX, stopY);
        ctx.lineTo(xX, stopY);
        ctx.stroke();
        ctx.setLineDash([]);

        // Stop label
        ctx.fillStyle = T.redBg;
        ctx.fillRect(eX - 1, stopY - 9, 78, 16);
        ctx.fillStyle = T.red;
        ctx.font = "bold 9px monospace";
        ctx.textAlign = "left";
        ctx.fillText(`STOP ${formatPrice(st.stopPrice)}`, eX + 3, stopY + 3);

        // TP1 level
        const tp1Y = priceToY(st.tp1Price);
        ctx.strokeStyle = T.blue;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(eX, tp1Y);
        ctx.lineTo(xX, tp1Y);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = T.blueDim;
        ctx.fillRect(eX - 1, tp1Y - 9, 72, 16);
        ctx.fillStyle = T.blue;
        ctx.font = "bold 9px monospace";
        ctx.fillText(`PT1 ${formatPrice(st.tp1Price)}`, eX + 3, tp1Y + 3);

        // TP2 level
        const tp2Y = priceToY(st.tp2Price);
        ctx.strokeStyle = T.purple;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(eX, tp2Y);
        ctx.lineTo(xX, tp2Y);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = T.purpleDim;
        ctx.fillRect(eX - 1, tp2Y - 9, 72, 16);
        ctx.fillStyle = T.purple;
        ctx.font = "bold 9px monospace";
        ctx.fillText(`PT2 ${formatPrice(st.tp2Price)}`, eX + 3, tp2Y + 3);

        // Entry level
        const entryY = priceToY(st.entryPrice);
        ctx.strokeStyle = T.textBright;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(eX, entryY);
        ctx.lineTo(xX, entryY);
        ctx.stroke();
        ctx.setLineDash([]);

        // Trade "path" line connecting entry to exit
        const exitY = priceToY(st.exitPrice);
        ctx.strokeStyle = st.pnl >= 0 ? T.green : T.red;
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = 0.6;
        ctx.beginPath();
        ctx.moveTo(eX, entryY);
        ctx.lineTo(xX, exitY);
        ctx.stroke();
        ctx.globalAlpha = 1;

        // Trade zone shading
        const topY = Math.min(entryY, exitY);
        const botY = Math.max(entryY, exitY);
        ctx.fillStyle = st.pnl >= 0 ? T.greenBg : T.redBg;
        ctx.fillRect(eX, topY, xX - eX, botY - topY);

        // Entry marker
        ctx.fillStyle = st.side === "long" ? T.green : T.red;
        ctx.beginPath();
        if (st.side === "long") {
          ctx.moveTo(eX, entryY + 8);
          ctx.lineTo(eX - 6, entryY + 16);
          ctx.lineTo(eX + 6, entryY + 16);
        } else {
          ctx.moveTo(eX, entryY - 8);
          ctx.lineTo(eX - 6, entryY - 16);
          ctx.lineTo(eX + 6, entryY - 16);
        }
        ctx.fill();

        // Exit marker
        ctx.fillStyle = st.pnl >= 0 ? T.green : T.red;
        ctx.beginPath();
        ctx.arc(xX, exitY, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = T.bg;
        ctx.beginPath();
        ctx.arc(xX, exitY, 2.5, 0, Math.PI * 2);
        ctx.fill();

        // Trade label
        const labelX = eX + (xX - eX) / 2;
        const labelY = st.side === "long" ? Math.min(tp1Y, entryY) - 20 : Math.max(stopY, entryY) + 30;
        const labelText = `${st.rMultiple > 0 ? "+" : ""}${st.rMultiple}R  $${st.pnl > 0 ? "+" : ""}${st.pnl.toFixed(0)}  ${st.exitType.replace(/_/g, " ").toUpperCase()}`;

        ctx.font = "bold 9px monospace";
        const lw = ctx.measureText(labelText).width + 12;
        ctx.fillStyle = st.pnl >= 0 ? "#0a2a1a" : "#2a0a0e";
        ctx.strokeStyle = st.pnl >= 0 ? T.green : T.red;
        ctx.lineWidth = 1;

        const rr = 4;
        const lx = labelX - lw / 2, ly = labelY - 8, lh = 18;
        ctx.beginPath();
        ctx.roundRect(lx, ly, lw, lh, rr);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = st.pnl >= 0 ? T.green : T.red;
        ctx.textAlign = "center";
        ctx.fillText(labelText, labelX, labelY + 4);
      }
    }

    // ── CANDLES
    visibleCandles.forEach((c, i) => {
      const x = (i / count) * chartW + (chartW / count - bodyW) / 2;
      const xCenter = (i / count) * chartW + (chartW / count) / 2;
      const isGreen = c.close >= c.open;
      const color = isGreen ? T.green : T.red;

      const oY = priceToY(c.open);
      const cY = priceToY(c.close);
      const hY = priceToY(c.high);
      const lY = priceToY(c.low);

      // Wick
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xCenter, hY);
      ctx.lineTo(xCenter, lY);
      ctx.stroke();

      // Body
      ctx.fillStyle = isGreen ? color : color;
      const bodyTop = Math.min(oY, cY);
      const bodyHeight = Math.max(1, Math.abs(oY - cY));
      if (isGreen) {
        ctx.fillStyle = T.bg;
        ctx.fillRect(x, bodyTop, bodyW, bodyHeight);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.strokeRect(x, bodyTop, bodyW, bodyHeight);
      } else {
        ctx.fillStyle = color;
        ctx.fillRect(x, bodyTop, bodyW, bodyHeight);
      }

      // Volume bars
      const vH = (c.volume / volRange.max) * (VOL_H - 8);
      vctx.fillStyle = isGreen ? T.greenDim : T.redDim;
      vctx.fillRect(x, VOL_H - vH, bodyW, vH);
    });

    // ── Non-selected trade markers (small)
    filteredTrades.forEach(t => {
      if (selectedTrade && t.id === selectedTrade.id) return;
      const entryIdx = candles.findIndex(c => c.time >= t.entryTime);
      if (entryIdx < viewStart || entryIdx >= viewEnd) return;

      const x = ((entryIdx - viewStart) / count) * chartW + (chartW / count) / 2;
      const y = priceToY(t.entryPrice);

      ctx.fillStyle = t.pnl >= 0 ? T.green : T.red;
      ctx.globalAlpha = 0.7;
      if (t.side === "long") {
        ctx.beginPath();
        ctx.moveTo(x, y + 4);
        ctx.lineTo(x - 4, y + 10);
        ctx.lineTo(x + 4, y + 10);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.moveTo(x, y - 4);
        ctx.lineTo(x - 4, y - 10);
        ctx.lineTo(x + 4, y - 10);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    });

    // ── Crosshair
    if (crosshairPos) {
      const { x, y } = crosshairPos;
      ctx.strokeStyle = T.crosshair;
      ctx.lineWidth = 0.5;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, CHART_H);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      ctx.stroke();
      ctx.setLineDash([]);

      // Price label on axis
      const price = pMin + ((CHART_H - y) / CHART_H) * pRange;
      ctx.fillStyle = T.bgActive;
      ctx.fillRect(chartW + 2, y - 10, PRICE_AXIS_W - 4, 20);
      ctx.fillStyle = T.textBright;
      ctx.font = "bold 11px monospace";
      ctx.textAlign = "right";
      ctx.fillText(formatPrice(price), w - 8, y + 4);
    }

    // ── Price axis border
    ctx.strokeStyle = T.border;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(chartW, 0);
    ctx.lineTo(chartW, CHART_H);
    ctx.stroke();

    vctx.strokeStyle = T.border;
    vctx.lineWidth = 1;
    vctx.beginPath();
    vctx.moveTo(chartW, 0);
    vctx.lineTo(chartW, VOL_H);
    vctx.stroke();

  }, [visibleCandles, priceRange, volRange, selectedTrade, filteredTrades, crosshairPos, candles, viewStart, viewEnd, priceToY, indexToX, getChartWidth]);

  // ── MOUSE HANDLERS ──────────────────────────────────────────
  const handleMouseMove = useCallback((e) => {
    const rect = chartRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const chartW = getChartWidth();
    if (x > chartW) { setCrosshairPos(null); setHoveredCandle(null); return; }

    setCrosshairPos({ x, y });

    const idx = xToIndex(x);
    if (idx >= 0 && idx < candles.length) {
      setHoveredCandle(candles[idx]);
    }

    if (isDragging.current) {
      const dx = e.clientX - dragStart.current;
      const pixelsPerCandle = chartW / (viewEnd - viewStart);
      const shift = Math.round(-dx / pixelsPerCandle);
      const newStart = Math.max(0, Math.min(candles.length - 50, dragViewStart.current + shift));
      const range = viewEnd - viewStart;
      setViewStart(newStart);
      setViewEnd(Math.min(candles.length, newStart + range));
    }
  }, [candles, viewStart, viewEnd, getChartWidth, xToIndex]);

  const handleMouseDown = useCallback((e) => {
    isDragging.current = true;
    dragStart.current = e.clientX;
    dragViewStart.current = viewStart;
    e.preventDefault();
  }, [viewStart]);

  const handleMouseUp = useCallback(() => {
    isDragging.current = false;
  }, []);

  const handleMouseLeave = useCallback(() => {
    setCrosshairPos(null);
    setHoveredCandle(null);
    isDragging.current = false;
  }, []);

  const handleWheel = useCallback((e) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 1.1 : 0.9;
    const range = viewEnd - viewStart;
    const newRange = Math.max(30, Math.min(candles.length, Math.round(range * delta)));

    const chartW = getChartWidth();
    const rect = chartRef.current?.getBoundingClientRect();
    const mouseX = rect ? (e.clientX - rect.left) / chartW : 0.5;

    const center = viewStart + range * mouseX;
    const newStart = Math.max(0, Math.round(center - newRange * mouseX));
    const newEnd = Math.min(candles.length, newStart + newRange);

    setViewStart(newStart);
    setViewEnd(newEnd);
  }, [candles, viewStart, viewEnd, getChartWidth]);

  // Focus on trade
  const focusTrade = useCallback((trade) => {
    setSelectedTrade(trade);
    const entryIdx = candles.findIndex(c => c.time >= trade.entryTime);
    const exitIdx = candles.findIndex(c => c.time >= trade.exitTime);
    const pad = 30;
    const newStart = Math.max(0, entryIdx - pad);
    const newEnd = Math.min(candles.length, exitIdx + pad);
    setViewStart(newStart);
    setViewEnd(newEnd);
  }, [candles]);

  // Stats
  const stats = useMemo(() => {
    const ft = filteredTrades;
    if (ft.length === 0) return null;
    const wins = ft.filter(t => t.pnl > 0);
    const losses = ft.filter(t => t.pnl <= 0);
    const totalPnl = ft.reduce((s, t) => s + t.pnl, 0);
    const grossWin = wins.reduce((s, t) => s + t.pnl, 0);
    const grossLoss = Math.abs(losses.reduce((s, t) => s + t.pnl, 0));
    return {
      count: ft.length,
      winRate: ((wins.length / ft.length) * 100).toFixed(1),
      pf: grossLoss > 0 ? (grossWin / grossLoss).toFixed(2) : "∞",
      totalPnl: totalPnl.toFixed(2),
      avgPnl: (totalPnl / ft.length).toFixed(2),
      avgWin: wins.length > 0 ? (grossWin / wins.length).toFixed(2) : "0",
      avgLoss: losses.length > 0 ? (grossLoss / losses.length).toFixed(2) : "0",
    };
  }, [filteredTrades]);

  if (loading) {
    return (
      <div style={{ background: T.bg, color: T.text, height: "100vh", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'SF Mono', monospace" }}>
        <div>Generating NQ market data & trade overlays...</div>
      </div>
    );
  }

  const exitColor = (type) => {
    const m = { pt1_partial: T.blue, pt2_target: T.purple, trailing_stop: T.green, stop_loss: T.red, be_plus: T.amber };
    return m[type] || T.text;
  };

  const regimeColor = (r) => {
    const m = { trending_up: T.green, trending_down: T.red, ranging: T.amber, unknown: T.textMuted };
    return m[r] || T.text;
  };

  return (
    <div style={{ background: T.bg, color: T.text, minHeight: "100vh", fontFamily: "'SF Mono', 'Cascadia Code', 'JetBrains Mono', 'Fira Code', monospace", fontSize: 12, overflow: "hidden" }}>

      {/* ── HEADER BAR */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 16px", borderBottom: `1px solid ${T.border}`, background: T.bgPanel }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span style={{ fontSize: 14, fontWeight: 700, color: T.textWhite, letterSpacing: 1 }}>NQ FORENSIC</span>
          <span style={{ color: T.textMuted, fontSize: 10, letterSpacing: 2 }}>MNQ · $2/pt · 2-LOT SCALE-OUT</span>
          <div style={{ display: "flex", gap: 2 }}>
            {["1m", "5m", "15m"].map(tf => (
              <button key={tf} onClick={() => setTimeframe(tf)} style={{
                background: timeframe === tf ? T.bgActive : "transparent", color: timeframe === tf ? T.textBright : T.textMuted,
                border: `1px solid ${timeframe === tf ? T.borderLight : "transparent"}`, padding: "2px 8px", borderRadius: 3, cursor: "pointer", fontSize: 10,
              }}>{tf}</button>
            ))}
          </div>
        </div>

        {hoveredCandle && (
          <div style={{ display: "flex", gap: 16, fontSize: 11 }}>
            <span style={{ color: T.textMuted }}>{formatFull(hoveredCandle.time)}</span>
            <span>O <span style={{ color: T.textBright }}>{formatPrice(hoveredCandle.open)}</span></span>
            <span>H <span style={{ color: T.green }}>{formatPrice(hoveredCandle.high)}</span></span>
            <span>L <span style={{ color: T.red }}>{formatPrice(hoveredCandle.low)}</span></span>
            <span>C <span style={{ color: hoveredCandle.close >= hoveredCandle.open ? T.green : T.red }}>{formatPrice(hoveredCandle.close)}</span></span>
            <span>Vol <span style={{ color: T.textBright }}>{hoveredCandle.volume.toLocaleString()}</span></span>
          </div>
        )}
      </div>

      <div style={{ display: "flex", height: "calc(100vh - 42px)" }}>

        {/* ── CHART AREA */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }} ref={containerRef}>

          {/* Main chart canvas */}
          <div style={{ position: "relative", cursor: isDragging.current ? "grabbing" : "crosshair" }}
            onMouseMove={handleMouseMove} onMouseDown={handleMouseDown}
            onMouseUp={handleMouseUp} onMouseLeave={handleMouseLeave}
            onWheel={handleWheel}
          >
            <canvas ref={chartRef} style={{ display: "block" }} />
          </div>

          {/* Volume canvas */}
          <div style={{ borderTop: `1px solid ${T.border}` }}>
            <canvas ref={volRef} style={{ display: "block" }} />
          </div>

          {/* ── STATS BAR */}
          {stats && (
            <div style={{ display: "flex", gap: 1, padding: 0, borderTop: `1px solid ${T.border}`, background: T.bgPanel }}>
              {[
                { label: "TRADES", value: stats.count, color: T.textBright },
                { label: "WIN%", value: `${stats.winRate}%`, color: parseFloat(stats.winRate) > 55 ? T.green : T.amber },
                { label: "PF", value: stats.pf, color: parseFloat(stats.pf) > 2 ? T.green : parseFloat(stats.pf) > 1.5 ? T.amber : T.red },
                { label: "TOTAL", value: `$${parseFloat(stats.totalPnl) >= 0 ? "+" : ""}${stats.totalPnl}`, color: parseFloat(stats.totalPnl) >= 0 ? T.green : T.red },
                { label: "AVG", value: `$${stats.avgPnl}`, color: parseFloat(stats.avgPnl) >= 0 ? T.green : T.red },
                { label: "AVG W", value: `$${stats.avgWin}`, color: T.green },
                { label: "AVG L", value: `-$${stats.avgLoss}`, color: T.red },
              ].map((s, i) => (
                <div key={i} style={{ flex: 1, padding: "6px 10px", background: T.bgCard, textAlign: "center" }}>
                  <div style={{ fontSize: 9, color: T.textMuted, letterSpacing: 1, marginBottom: 2 }}>{s.label}</div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: s.color }}>{s.value}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── SIDEBAR: FILTERS + TRADE TABLE */}
        <div style={{ width: 340, borderLeft: `1px solid ${T.border}`, display: "flex", flexDirection: "column", background: T.bgPanel }}>

          {/* Filters */}
          <div style={{ padding: "10px 12px", borderBottom: `1px solid ${T.border}` }}>
            <div style={{ fontSize: 9, color: T.textMuted, letterSpacing: 2, marginBottom: 8 }}>FILTERS</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <select value={filterRegime} onChange={e => setFilterRegime(e.target.value)}
                style={{ background: T.bgCard, color: T.textBright, border: `1px solid ${T.border}`, borderRadius: 3, padding: "3px 6px", fontSize: 10, flex: 1, minWidth: 80 }}>
                <option value="all">All Regimes</option>
                <option value="trending_up">Trend Up</option>
                <option value="trending_down">Trend Down</option>
                <option value="ranging">Ranging</option>
                <option value="unknown">Unknown</option>
              </select>
              <select value={filterSide} onChange={e => setFilterSide(e.target.value)}
                style={{ background: T.bgCard, color: T.textBright, border: `1px solid ${T.border}`, borderRadius: 3, padding: "3px 6px", fontSize: 10, flex: 1, minWidth: 60 }}>
                <option value="all">Both</option>
                <option value="long">Long</option>
                <option value="short">Short</option>
              </select>
              <select value={filterExit} onChange={e => setFilterExit(e.target.value)}
                style={{ background: T.bgCard, color: T.textBright, border: `1px solid ${T.border}`, borderRadius: 3, padding: "3px 6px", fontSize: 10, flex: 1, minWidth: 80 }}>
                <option value="all">All Exits</option>
                <option value="pt1_partial">PT1 Partial</option>
                <option value="pt2_target">PT2 Target</option>
                <option value="trailing_stop">Trailing</option>
                <option value="stop_loss">Stop Loss</option>
                <option value="be_plus">BE+</option>
              </select>
            </div>
            <div style={{ marginTop: 6, display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 10, color: T.textMuted }}>Score ≥</span>
              <input type="range" min="0.70" max="0.98" step="0.01" value={filterMinScore}
                onChange={e => setFilterMinScore(parseFloat(e.target.value))}
                style={{ flex: 1, accentColor: T.blue }} />
              <span style={{ fontSize: 11, color: T.textBright, fontWeight: 600, minWidth: 36 }}>{filterMinScore.toFixed(2)}</span>
            </div>
          </div>

          {/* Selected trade detail */}
          {selectedTrade && (
            <div style={{ padding: "10px 12px", borderBottom: `1px solid ${T.border}`, background: T.bgCard }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: T.textWhite, letterSpacing: 1 }}>TRADE #{selectedTrade.id}</span>
                <button onClick={() => setSelectedTrade(null)} style={{ background: "transparent", border: "none", color: T.textMuted, cursor: "pointer", fontSize: 12 }}>✕</button>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px", fontSize: 10 }}>
                <div><span style={{ color: T.textMuted }}>Side</span> <span style={{ color: selectedTrade.side === "long" ? T.green : T.red, fontWeight: 600 }}>{selectedTrade.side.toUpperCase()}</span></div>
                <div><span style={{ color: T.textMuted }}>Score</span> <span style={{ color: T.blue, fontWeight: 600 }}>{selectedTrade.signalScore.toFixed(3)}</span></div>
                <div><span style={{ color: T.textMuted }}>Entry</span> <span style={{ color: T.textBright }}>{formatPrice(selectedTrade.entryPrice)}</span></div>
                <div><span style={{ color: T.textMuted }}>Exit</span> <span style={{ color: T.textBright }}>{formatPrice(selectedTrade.exitPrice)}</span></div>
                <div><span style={{ color: T.textMuted }}>Stop</span> <span style={{ color: T.red }}>{formatPrice(selectedTrade.stopPrice)} ({selectedTrade.stopDist.toFixed(1)}pt)</span></div>
                <div><span style={{ color: T.textMuted }}>R:R</span> <span style={{ color: selectedTrade.rMultiple > 0 ? T.green : T.red, fontWeight: 600 }}>{selectedTrade.rMultiple > 0 ? "+" : ""}{selectedTrade.rMultiple}R</span></div>
                <div><span style={{ color: T.textMuted }}>PnL</span> <span style={{ color: selectedTrade.pnl >= 0 ? T.green : T.red, fontWeight: 700 }}>${selectedTrade.pnl >= 0 ? "+" : ""}{selectedTrade.pnl.toFixed(2)}</span></div>
                <div><span style={{ color: T.textMuted }}>Slip</span> <span style={{ color: T.amber }}>${selectedTrade.slippage.toFixed(2)}</span></div>
                <div><span style={{ color: T.textMuted }}>C1</span> <span style={{ color: selectedTrade.c1Pnl >= 0 ? T.green : T.red }}>${selectedTrade.c1Pnl.toFixed(2)}</span></div>
                <div><span style={{ color: T.textMuted }}>C2</span> <span style={{ color: selectedTrade.c2Pnl >= 0 ? T.green : T.red }}>${selectedTrade.c2Pnl.toFixed(2)}</span></div>
                <div><span style={{ color: T.textMuted }}>MFE</span> <span style={{ color: T.green }}>{selectedTrade.mfe.toFixed(1)}pt</span></div>
                <div><span style={{ color: T.textMuted }}>MAE</span> <span style={{ color: T.red }}>{selectedTrade.mae.toFixed(1)}pt</span></div>
                <div><span style={{ color: T.textMuted }}>Regime</span> <span style={{ color: regimeColor(selectedTrade.regime) }}>{selectedTrade.regime}</span></div>
                <div><span style={{ color: T.textMuted }}>HTF</span> <span style={{ color: T.textBright }}>{selectedTrade.htfBias}</span></div>
                <div style={{ gridColumn: "1 / -1" }}><span style={{ color: T.textMuted }}>Exit</span> <span style={{ color: exitColor(selectedTrade.exitType), fontWeight: 600 }}>{selectedTrade.exitType.replace(/_/g, " ").toUpperCase()}</span></div>
                <div style={{ gridColumn: "1 / -1" }}><span style={{ color: T.textMuted }}>Hold</span> <span style={{ color: T.textBright }}>{selectedTrade.holdBars} bars</span> <span style={{ color: T.textMuted, marginLeft: 8 }}>{formatFull(selectedTrade.entryTime)}</span></div>
                <div style={{ gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: 4, marginTop: 2 }}>
                  <span style={{ color: T.textMuted }}>Compliance</span>
                  <span style={{ background: selectedTrade.isCompliant ? "#0a2a1a" : "#2a0a0e", color: selectedTrade.isCompliant ? T.green : T.red, padding: "1px 6px", borderRadius: 3, fontSize: 9, fontWeight: 700 }}>
                    {selectedTrade.isCompliant ? "✓ PASS" : "✗ VIOLATION"}
                  </span>
                </div>
              </div>
            </div>
          )}

          {/* Trade table */}
          <div style={{ flex: 1, overflow: "auto" }}>
            <div style={{ padding: "8px 12px 4px", fontSize: 9, color: T.textMuted, letterSpacing: 2 }}>
              TRADE LOG — {filteredTrades.length} TRADES
            </div>
            <div style={{ fontSize: 10 }}>
              {/* Header */}
              <div style={{ display: "grid", gridTemplateColumns: "28px 36px 52px 48px 48px 56px 48px", gap: 2, padding: "4px 12px", borderBottom: `1px solid ${T.border}`, color: T.textMuted, fontSize: 9, letterSpacing: 0.5, position: "sticky", top: 0, background: T.bgPanel, zIndex: 1 }}>
                <span>#</span><span>SIDE</span><span>ENTRY</span><span>STOP</span><span>PNL</span><span>EXIT</span><span>REGIME</span>
              </div>
              {filteredTrades.map(t => (
                <div key={t.id} onClick={() => focusTrade(t)}
                  style={{
                    display: "grid", gridTemplateColumns: "28px 36px 52px 48px 48px 56px 48px", gap: 2,
                    padding: "5px 12px", cursor: "pointer", borderBottom: `1px solid ${T.border}08`,
                    background: selectedTrade?.id === t.id ? T.bgActive : "transparent",
                    transition: "background 0.15s",
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = selectedTrade?.id === t.id ? T.bgActive : T.bgHover}
                  onMouseLeave={e => e.currentTarget.style.background = selectedTrade?.id === t.id ? T.bgActive : "transparent"}
                >
                  <span style={{ color: T.textMuted }}>{t.id}</span>
                  <span style={{ color: t.side === "long" ? T.green : T.red, fontWeight: 600 }}>{t.side === "long" ? "LNG" : "SHT"}</span>
                  <span style={{ color: T.textBright }}>{formatPrice(t.entryPrice)}</span>
                  <span style={{ color: T.textMuted }}>{t.stopDist.toFixed(0)}pt</span>
                  <span style={{ color: t.pnl >= 0 ? T.green : T.red, fontWeight: 600 }}>{t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(0)}</span>
                  <span style={{ color: exitColor(t.exitType), fontSize: 8 }}>{t.exitType.replace(/_/g, " ").split(" ").map(w => w[0].toUpperCase()).join("")}</span>
                  <span style={{ fontSize: 8 }}>
                    <span style={{ display: "inline-block", width: 5, height: 5, borderRadius: "50%", background: regimeColor(t.regime), marginRight: 3 }}></span>
                    {t.regime.replace("trending_", "T").replace("unknown", "UNK").replace("ranging", "RNG").substring(0, 3).toUpperCase()}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Legend */}
          <div style={{ padding: "8px 12px", borderTop: `1px solid ${T.border}`, fontSize: 9, color: T.textMuted }}>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <span><span style={{ color: T.blue }}>●</span> PT1</span>
              <span><span style={{ color: T.purple }}>●</span> PT2</span>
              <span><span style={{ color: T.green }}>●</span> Trail</span>
              <span><span style={{ color: T.red }}>●</span> Stop</span>
              <span><span style={{ color: T.amber }}>●</span> BE+</span>
            </div>
            <div style={{ marginTop: 4, color: T.textMuted, fontSize: 8 }}>Scroll to zoom · Drag to pan · Click trade to focus</div>
          </div>
        </div>
      </div>
    </div>
  );
}
