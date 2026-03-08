import { useState, useEffect } from "react";

// ── Color Palette: White + Modern Brown ──────────────────────────
const THEME = {
  bg: "#FDFBF9",
  surface: "#FFFFFF",
  surfaceAlt: "#FAF7F4",
  border: "#E8DDD3",
  borderLight: "#F0EAE3",
  text: "#3D2E22",
  textSecondary: "#8B7355",
  textMuted: "#B8A48E",
  accent: "#8B6914",
  accentLight: "#C49A2A",
  accentSoft: "#F5ECD7",
  brown: {
    900: "#3D2E22",
    700: "#5C4333",
    500: "#8B7355",
    400: "#A89070",
    300: "#C4AD93",
    200: "#DDD0C0",
    100: "#EDE5DA",
    50:  "#F7F3EE",
  },
};

const P_COLORS = {
  High:   { bg: "#FCDDD5", text: "#B5301A", border: "#E8A090", dot: "#D94415", cardBg: "#FDE8E2", cardBorder: "#EAADA0" },
  Medium: { bg: "#FCEDC5", text: "#7A5508", border: "#E2C06A", dot: "#B08515", cardBg: "#FDF2D0", cardBorder: "#E4C878" },
  Low:    { bg: "#D5EDDC", text: "#1E6B3A", border: "#88CCA0", dot: "#2D8C4E", cardBg: "#DEEEE3", cardBorder: "#96D1A8" },
};

const DONE_CARD = { cardBg: "#ECFAEF", cardBorder: "#B8E4C4", dot: "#5CB578" };

const COL_STYLES = {
  "To Do":       { accent: THEME.brown[500], label: "#8B7355", icon: "○" },
  "In Progress": { accent: THEME.accent,     label: "#8B6914", icon: "◑" },
  "Done":        { accent: "#3DA66A",        label: "#2D7A4F", icon: "●" },
};

const COLS = ["To Do", "In Progress", "Done"];
const PRIORITIES = ["High", "Medium", "Low"];

// ── Tasks: Updated to reflect actual project state (Mar 2026) ───
const DEFAULT_TASKS = [
  // ─── TO DO ───
  {
    id: "td1", col: "To Do", priority: "High",
    title: "IBKR Auto-Launch & Self-Authentication",
    desc: "Build fully automated startup pipeline: headless IBKR Gateway launch, credential injection, session keepalive, health-check polling, and relaunch-on-failure."
  },
  {
    id: "td2", col: "To Do", priority: "High",
    title: "Test C2 Trail at 2.5× ATR",
    desc: "Current C2 runner uses 2.0× ATR trail. Backtest 2.5× multiplier to determine if wider trail captures more runner profit without giving back too much."
  },
  {
    id: "td3", col: "To Do", priority: "High",
    title: "Paper Trade 500+ Trades Validation",
    desc: "Deploy bot against Tradovate demo account. Accumulate 500+ paper trades to validate backtest metrics (PF 1.73, 61.9% WR) under live market conditions."
  },
  {
    id: "td4", col: "To Do", priority: "Medium",
    title: "Regime-Adaptive Exits (Post Walk-Forward)",
    desc: "Enable regime-aware C1/C2 exit parameters after walk-forward validation confirms stability. Trending markets get wider trails, choppy markets get tighter exits."
  },
  {
    id: "td5", col: "To Do", priority: "Medium",
    title: "HAR-RV Volatility Forecasting Module",
    desc: "Implement Corsi (2009) HAR-RV model from 5-min MNQ returns. Rolling daily/weekly/monthly RV components. Replace ATR-only stop sizing with model-informed dynamic sizing."
  },
  {
    id: "td6", col: "To Do", priority: "Medium",
    title: "High-Volatility Regime Filter Investigation",
    desc: "Investigate before implementing. Must assess impact on fat-tail capture — filters that improve average-trade metrics can destroy PnL by clipping rare large winners."
  },

  // ─── IN PROGRESS ───
  {
    id: "ip1", col: "In Progress", priority: "High",
    title: "7-Period Parallel Backtest (Running)",
    desc: "All 7 periods (Sep 2021–Aug 2025, ~1.44M bars) running in parallel via parallel_backtest.py. 8 workers active including dual-worker verification on Period 7. ETA ~3 hours."
  },
  {
    id: "ip2", col: "In Progress", priority: "High",
    title: "IBKR Live Data Connector",
    desc: "Sub-modules partially built. Complete WebSocket feed integration, ensure process_bar() interface parity between historical CSV replay and live feed."
  },

  // ─── DONE ───
  {
    id: "d1", col: "Done", priority: "High",
    title: "Causality Audit + OB/FVG Look-Ahead Fix",
    desc: "Comprehensive audit found OB and FVG detection used current bar as displacement confirmation (look-ahead bias). Fixed by shifting detection window back by 1 bar in features/engine.py. 9 other areas verified strictly causal."
  },
  {
    id: "d2", col: "Done", priority: "High",
    title: "Parallel Multi-Period Backtest Runner",
    desc: "Built parallel_backtest.py — Python multiprocessing engine running all 7 periods simultaneously (~3hr vs ~15hr sequential). Includes TradingView data loader, SHA-256 integrity, dual-worker determinism for 12-month periods."
  },
  {
    id: "d3", col: "Done", priority: "High",
    title: "Continuous Data Coverage (Sep 2021–Aug 2025)",
    desc: "Filled former Feb–Aug 2023 data gap. 7 periods now cover 4 continuous years with ~1.44M 1-minute bars from TradingView exports. Identified and handled mislabeled data files."
  },
  {
    id: "d4", col: "Done", priority: "High",
    title: "CausalReplayEngine + HTF Causal Delivery",
    desc: "Built CausalReplayEngine for strict bar-by-bar replay. Signal at bar N → entry at bar N+1 open + adversarial slippage. HTF bars only fed to engine AFTER completion (T + tf_minutes). Zero look-ahead."
  },
  {
    id: "d5", col: "Done", priority: "High",
    title: "C1 Trail-from-Profit Exit (Variant C)",
    desc: "Replaced old Time 10 bars exit. C1 activates 2.5pt trailing stop once unrealized profit >= 3.0pts. Validated across 6/6 months profitable with calibrated slippage. PF 1.61."
  },
  {
    id: "d6", col: "Done", priority: "High",
    title: "2-Contract Scale-Out System",
    desc: "C1 (time-based exit) + C2 (ATR trailing runner). C2 captures ~60% of total PnL via fat-tail runners. BE buffer +2pts avoids stop-hunting. Full lifecycle in ScaleOutExecutor."
  },
  {
    id: "d7", col: "Done", priority: "High",
    title: "High-Conviction Filter (Score ≥ 0.75 + Stop ≤ 30pts)",
    desc: "Two non-negotiable hard gates enforced in main.py. Only intersection of tight stops + strong signals produces durable edge. HTF strength gate >= 0.3 (Config D)."
  },
  {
    id: "d8", col: "Done", priority: "High",
    title: "Multi-Timeframe HTF Bias Engine",
    desc: "6 timeframes (5m/15m/30m/1H/4H/1D) built causally from 1m data. Directional consensus gates all entries. Staleness limits per TF. Powers the HTF strength gate."
  },
  {
    id: "d9", col: "Done", priority: "High",
    title: "Liquidity Sweep Detector",
    desc: "Institutional stop-hunt detection at PDH/PDL, session H/L, PWH/PWL, VWAP, round numbers. Three entry modes: signal-only, sweep-only (≥0.70), confluence (+0.05 boost). Additive — never replaces existing signals."
  },
  {
    id: "d10", col: "Done", priority: "High",
    title: "UCL v2 Implementation",
    desc: "Score 0.60–0.74 → watch state (RECLAIM → FVG_FORM → FVG_TAP). Score ≥0.75 → immediate entry with FVG boost (+0.05). Wide-stop sweeps → tight-stop conversion."
  },
  {
    id: "d11", col: "Done", priority: "High",
    title: "Kill Switch Redesign (Removed Consecutive-Loss)",
    desc: "Forensic analysis proved consecutive-loss kill switch was net negative — clipped recovery trades. Removed entirely. Daily drawdown limit retained as only kill switch."
  },
  {
    id: "d12", col: "Done", priority: "High",
    title: "C2 Delayed Breakeven (Variant B)",
    desc: "C2 BE stop activates after delay instead of immediately. Prevents premature stop-outs on pullbacks. +2pt BE buffer to survive stop-hunting wicks."
  },
  {
    id: "d13", col: "Done", priority: "Medium",
    title: "607-Trade Profile Analysis",
    desc: "Fat-tail dependency confirmed: top 10% of trades = ~241% of profit. C2 runner = ~60% total PnL. Sweep PF ~1.74 vs. confluence PF ~0.99. Data-driven architecture decisions."
  },
  {
    id: "d14", col: "Done", priority: "Medium",
    title: "Security Audit (3 Phases)",
    desc: "Full security audit across API keys, authentication, data pipeline, and broker connections. All findings documented in docs/SECURITY_AUDIT.md."
  },
  {
    id: "d15", col: "Done", priority: "Medium",
    title: "Dashboard + Website (GitHub Pages)",
    desc: "Live dashboard with trade metrics, kanban board, and weekly reports deployed to samueldagostino.github.io/AI-Trading-Bot/. Real-time stats JSON feed."
  },
];

function genId() { return "t" + Date.now() + Math.random().toString(36).slice(2, 6); }

const STORAGE_KEY = "algo-kanban-v2";

export default function App() {
  const [tasks, setTasks] = useState(DEFAULT_TASKS);
  const [loaded, setLoaded] = useState(false);
  const [dragging, setDragging] = useState(null);
  const [dragOver, setDragOver] = useState(null);
  const [modal, setModal] = useState(null);
  const [doneExpanded, setDoneExpanded] = useState(false);
  const DONE_PREVIEW = 5;

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) setTasks(JSON.parse(stored));
    } catch (_) {}
    setLoaded(true);
  }, []);

  useEffect(() => {
    if (!loaded) return;
    const t = setTimeout(() => {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks)); } catch (_) {}
    }, 600);
    return () => clearTimeout(t);
  }, [tasks, loaded]);

  const moveTask = (id, col) => setTasks(prev => prev.map(t => t.id === id ? { ...t, col } : t));
  const saveTask = (task) => {
    setTasks(prev => {
      const exists = prev.find(t => t.id === task.id);
      return exists ? prev.map(t => t.id === task.id ? task : t) : [...prev, task];
    });
    setModal(null);
  };
  const deleteTask = (id) => { setTasks(prev => prev.filter(t => t.id !== id)); setModal(null); };

  const handleDrop = (col) => {
    if (dragging && dragging !== col) moveTask(dragging, col);
    setDragging(null); setDragOver(null);
  };

  // Card component
  const TaskCard = ({ task }) => (
    <div
      draggable
      onDragStart={() => setDragging(task.id)}
      onDragEnd={() => { setDragging(null); setDragOver(null); }}
      onClick={() => setModal({ mode: "edit", task: { ...task } })}
      style={{
        background: task.col === "Done" ? DONE_CARD.cardBg : P_COLORS[task.priority].cardBg,
        border: `1px solid ${task.col === "Done" ? DONE_CARD.cardBorder : P_COLORS[task.priority].cardBorder}`,
        borderLeft: `4px solid ${task.col === "Done" ? DONE_CARD.dot : P_COLORS[task.priority].dot}`,
        borderRadius: 10,
        padding: "14px 16px",
        marginBottom: 10,
        cursor: "grab",
        transition: "all 0.2s ease",
        boxShadow: "0 1px 3px rgba(61,46,34,0.04)",
      }}
      onMouseEnter={e => { e.currentTarget.style.boxShadow = "0 4px 12px rgba(61,46,34,0.08)"; e.currentTarget.style.transform = "translateY(-1px)"; }}
      onMouseLeave={e => { e.currentTarget.style.boxShadow = "0 1px 3px rgba(61,46,34,0.04)"; e.currentTarget.style.transform = "translateY(0)"; }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: THEME.text, lineHeight: 1.5, flex: 1 }}>{task.title}</div>
        {task.col === "Done" ? (
          <span style={{
            fontSize: 11,
            color: "#2D7A4F",
            background: "#EDF6F0",
            border: "1px solid #A8D5B8",
            borderRadius: 4,
            padding: "2px 8px",
            whiteSpace: "nowrap",
            fontWeight: 600,
          }}>✓</span>
        ) : (
          <span style={{
            fontSize: 9,
            color: P_COLORS[task.priority].text,
            background: P_COLORS[task.priority].bg,
            border: `1px solid ${P_COLORS[task.priority].border}`,
            borderRadius: 4,
            padding: "2px 7px",
            whiteSpace: "nowrap",
            letterSpacing: 1,
            fontWeight: 600,
            fontFamily: "'Inter', system-ui, sans-serif",
          }}>{task.priority.toUpperCase()}</span>
        )}
      </div>
      {task.desc && (
        <div style={{
          fontSize: 11.5,
          color: THEME.textSecondary,
          marginTop: 8,
          lineHeight: 1.6,
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}>{task.desc}</div>
      )}
    </div>
  );

  return (
    <div style={{
      minHeight: "100vh",
      background: THEME.bg,
      color: THEME.text,
      fontFamily: "'Inter', system-ui, -apple-system, sans-serif",
      padding: "32px 24px",
    }}>
      {/* ── Header ── */}
      <div style={{ maxWidth: 1400, margin: "0 auto 28px", display: "flex", alignItems: "flex-end", justifyContent: "space-between", flexWrap: "wrap", gap: 16 }}>
        <div>
          <div style={{
            fontSize: 10.5,
            color: THEME.accent,
            letterSpacing: 3.5,
            textTransform: "uppercase",
            fontWeight: 600,
            marginBottom: 6,
          }}>NQ BOT SYSTEM BUILD</div>
          <h1 style={{
            margin: 0,
            fontSize: 26,
            fontWeight: 700,
            color: THEME.brown[900],
            letterSpacing: -0.5,
          }}>Algo Trading To-Do List</h1>
        </div>
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <div style={{
            fontSize: 11,
            color: THEME.textMuted,
            background: THEME.surfaceAlt,
            border: `1px solid ${THEME.borderLight}`,
            borderRadius: 8,
            padding: "8px 14px",
            fontWeight: 500,
          }}>
            <span style={{ color: "#3DA66A", marginRight: 6 }}>●</span>
            {tasks.filter(t => t.col === "Done").length} completed
            <span style={{ margin: "0 8px", color: THEME.border }}>|</span>
            <span style={{ color: THEME.accent, marginRight: 6 }}>◑</span>
            {tasks.filter(t => t.col === "In Progress").length} active
            <span style={{ margin: "0 8px", color: THEME.border }}>|</span>
            <span style={{ color: THEME.brown[400], marginRight: 6 }}>○</span>
            {tasks.filter(t => t.col === "To Do").length} queued
          </div>
        </div>
      </div>

      {/* ── Columns ── */}
      <div style={{ maxWidth: 1400, margin: "0 auto", display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 20 }}>
        {COLS.map(col => {
          const allColTasks = tasks.filter(t => t.col === col);
          const isDone = col === "Done";
          const visibleTasks = isDone && !doneExpanded ? allColTasks.slice(0, DONE_PREVIEW) : allColTasks;
          const hiddenCount = isDone ? allColTasks.length - DONE_PREVIEW : 0;
          const isOver = dragOver === col;
          const style = COL_STYLES[col];

          return (
            <div key={col}
              onDragOver={e => { e.preventDefault(); setDragOver(col); }}
              onDragLeave={() => setDragOver(null)}
              onDrop={() => handleDrop(col)}
              style={{
                background: isOver ? THEME.accentSoft : THEME.surfaceAlt,
                border: `1px solid ${isOver ? THEME.accent : THEME.borderLight}`,
                borderRadius: 14,
                padding: 18,
                minHeight: 420,
                transition: "all 0.2s ease",
              }}
            >
              {/* Column Header */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, paddingBottom: 12, borderBottom: `1px solid ${THEME.borderLight}` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ fontSize: 14, color: style.accent }}>{style.icon}</span>
                  <span style={{
                    fontSize: 11.5,
                    letterSpacing: 2,
                    color: style.label,
                    textTransform: "uppercase",
                    fontWeight: 700,
                  }}>{col}</span>
                  <span style={{
                    fontSize: 11,
                    background: THEME.brown[100],
                    color: THEME.brown[500],
                    borderRadius: 12,
                    padding: "1px 9px",
                    fontWeight: 600,
                  }}>{allColTasks.length}</span>
                </div>
                <button
                  onClick={() => setModal({ mode: "add", task: { id: genId(), col, priority: "Medium", title: "", desc: "" } })}
                  style={{
                    background: THEME.surface,
                    border: `1px solid ${THEME.border}`,
                    color: THEME.brown[400],
                    borderRadius: 6,
                    width: 26,
                    height: 26,
                    cursor: "pointer",
                    fontSize: 16,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    transition: "all 0.15s",
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = THEME.accentSoft; e.currentTarget.style.color = THEME.accent; }}
                  onMouseLeave={e => { e.currentTarget.style.background = THEME.surface; e.currentTarget.style.color = THEME.brown[400]; }}
                >+</button>
              </div>

              {/* Cards */}
              {visibleTasks.map(task => <TaskCard key={task.id} task={task} />)}

              {/* Done: Show More / Show Less */}
              {isDone && hiddenCount > 0 && (
                <button
                  onClick={() => setDoneExpanded(!doneExpanded)}
                  style={{
                    width: "100%",
                    background: doneExpanded ? THEME.surface : "linear-gradient(135deg, #F5ECD7 0%, #EDE5DA 100%)",
                    border: `1px solid ${THEME.border}`,
                    borderRadius: 10,
                    padding: "12px 16px",
                    cursor: "pointer",
                    fontSize: 12,
                    fontWeight: 600,
                    color: THEME.accent,
                    letterSpacing: 0.5,
                    marginTop: 4,
                    transition: "all 0.2s",
                    fontFamily: "inherit",
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = THEME.accentSoft; }}
                  onMouseLeave={e => { e.currentTarget.style.background = doneExpanded ? THEME.surface : "linear-gradient(135deg, #F5ECD7 0%, #EDE5DA 100%)"; }}
                >
                  {doneExpanded
                    ? "Show Less ▲"
                    : `Show ${hiddenCount} More ▼`
                  }
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Footer ── */}
      <div style={{ maxWidth: 1400, margin: "24px auto 0", textAlign: "center" }}>
        <div style={{ fontSize: 10, color: THEME.textMuted, letterSpacing: 2 }}>
          NW TRADING SYSTEMS — MNQ FUTURES
        </div>
      </div>

      {/* ── Modal ── */}
      {modal && <TaskModal modal={modal} onSave={saveTask} onDelete={deleteTask} onClose={() => setModal(null)} />}
    </div>
  );
}

function TaskModal({ modal, onSave, onDelete, onClose }) {
  const [task, setTask] = useState(modal.task);
  const set = (k, v) => setTask(p => ({ ...p, [k]: v }));

  return (
    <div
      style={{ position: "fixed", inset: 0, background: "rgba(61,46,34,0.4)", backdropFilter: "blur(4px)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100, padding: 16 }}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div style={{
        background: THEME.surface,
        border: `1px solid ${THEME.border}`,
        borderRadius: 16,
        padding: 28,
        width: "100%",
        maxWidth: 520,
        boxShadow: "0 20px 60px rgba(61,46,34,0.15)",
      }}>
        <div style={{ fontSize: 11, color: THEME.accent, letterSpacing: 2.5, marginBottom: 20, fontWeight: 600 }}>
          {modal.mode === "add" ? "NEW TASK" : "EDIT TASK"}
        </div>
        <input
          value={task.title}
          onChange={e => set("title", e.target.value)}
          placeholder="Task title..."
          style={{
            width: "100%",
            background: THEME.surfaceAlt,
            border: `1px solid ${THEME.borderLight}`,
            color: THEME.text,
            borderRadius: 10,
            padding: "10px 14px",
            fontSize: 14,
            marginBottom: 14,
            boxSizing: "border-box",
            fontFamily: "inherit",
            outline: "none",
          }}
          onFocus={e => e.currentTarget.style.borderColor = THEME.accent}
          onBlur={e => e.currentTarget.style.borderColor = THEME.borderLight}
        />
        <textarea
          value={task.desc}
          onChange={e => set("desc", e.target.value)}
          placeholder="Description..."
          rows={4}
          style={{
            width: "100%",
            background: THEME.surfaceAlt,
            border: `1px solid ${THEME.borderLight}`,
            color: THEME.text,
            borderRadius: 10,
            padding: "10px 14px",
            fontSize: 12.5,
            marginBottom: 14,
            boxSizing: "border-box",
            fontFamily: "inherit",
            resize: "vertical",
            outline: "none",
            lineHeight: 1.6,
          }}
          onFocus={e => e.currentTarget.style.borderColor = THEME.accent}
          onBlur={e => e.currentTarget.style.borderColor = THEME.borderLight}
        />
        <div style={{ display: "flex", gap: 12, marginBottom: 24, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 120 }}>
            <div style={{ fontSize: 10, color: THEME.textMuted, letterSpacing: 1.5, marginBottom: 6, fontWeight: 600 }}>PRIORITY</div>
            <select
              value={task.priority}
              onChange={e => set("priority", e.target.value)}
              style={{
                width: "100%",
                background: THEME.surfaceAlt,
                border: `1px solid ${THEME.borderLight}`,
                color: THEME.text,
                borderRadius: 10,
                padding: "8px 12px",
                fontSize: 12.5,
                fontFamily: "inherit",
              }}
            >
              {PRIORITIES.map(p => <option key={p}>{p}</option>)}
            </select>
          </div>
          <div style={{ flex: 1, minWidth: 120 }}>
            <div style={{ fontSize: 10, color: THEME.textMuted, letterSpacing: 1.5, marginBottom: 6, fontWeight: 600 }}>COLUMN</div>
            <select
              value={task.col}
              onChange={e => set("col", e.target.value)}
              style={{
                width: "100%",
                background: THEME.surfaceAlt,
                border: `1px solid ${THEME.borderLight}`,
                color: THEME.text,
                borderRadius: 10,
                padding: "8px 12px",
                fontSize: 12.5,
                fontFamily: "inherit",
              }}
            >
              {COLS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          {modal.mode === "edit"
            ? <button
                onClick={() => onDelete(task.id)}
                style={{
                  background: "none",
                  border: "1px solid #E25822",
                  color: "#E25822",
                  borderRadius: 10,
                  padding: "9px 18px",
                  fontSize: 12,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  fontWeight: 600,
                  letterSpacing: 0.5,
                  transition: "all 0.15s",
                }}
              >DELETE</button>
            : <div />}
          <div style={{ display: "flex", gap: 10 }}>
            <button
              onClick={onClose}
              style={{
                background: "none",
                border: `1px solid ${THEME.border}`,
                color: THEME.textSecondary,
                borderRadius: 10,
                padding: "9px 18px",
                fontSize: 12,
                cursor: "pointer",
                fontFamily: "inherit",
                fontWeight: 600,
                letterSpacing: 0.5,
              }}
            >CANCEL</button>
            <button
              onClick={() => onSave(task)}
              disabled={!task.title.trim()}
              style={{
                background: task.title.trim() ? THEME.accent : THEME.brown[200],
                color: "#FFF",
                border: "none",
                borderRadius: 10,
                padding: "9px 20px",
                fontSize: 12,
                cursor: task.title.trim() ? "pointer" : "not-allowed",
                fontFamily: "inherit",
                fontWeight: 600,
                letterSpacing: 0.5,
                transition: "all 0.15s",
              }}
            >SAVE</button>
          </div>
        </div>
      </div>
    </div>
  );
}
