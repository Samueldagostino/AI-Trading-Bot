import { useState, useEffect, useRef } from "react";

const COLS = ["To Do", "In Progress", "Done"];
const PRIORITIES = ["High", "Medium", "Low"];
const P_COLOR = { High: "#ef4444", Medium: "#f59e0b", Low: "#22c55e" };

const DEFAULT_TASKS = [
  {
    id: "t1", col: "To Do", priority: "High",
    title: "IBKR Auto-Launch & Self-Authentication System",
    desc: "Build a fully automated startup pipeline: headless IBKR Client Portal Gateway launch, credential injection, session keepalive, health-check polling, and relaunch-on-failure logic. Bot must be able to cold-start and re-authenticate without manual intervention."
  },
  {
    id: "t2", col: "To Do", priority: "High",
    title: "HTF-Bearish Long Filter Correction",
    desc: "Implement highest-confidence logic correction: block long entries when HTF bias is bearish. This is a logic fix, not an optimisation — implement immediately."
  },
  {
    id: "t3", col: "To Do", priority: "High",
    title: "Max Stop Gate Forensic Analysis",
    desc: "Determine why 30pt stop cap is blocking high-PF trades (PF ~3.00, avg MFE ~66pts). Compare stop formula output vs. structural stop below sweep low on blocked trade subset."
  },
  {
    id: "t4", col: "To Do", priority: "High",
    title: "Multi-Signal Architecture Audit",
    desc: "Map all 14 knowledge base concepts against implementation status. Design four-layer decision engine: HTF Gate → Signal Detection → Confluence Scoring → UCL Routing."
  },
  {
    id: "t5", col: "To Do", priority: "Medium",
    title: "High-Volatility Regime Filter Investigation",
    desc: "Investigate before implementing. Must assess impact on fat-tail capture — filters that improve average-trade metrics can destroy PnL by clipping rare large winners."
  },
  {
    id: "t6", col: "To Do", priority: "Medium",
    title: "FVG Detector Integration into Confluence Engine",
    desc: "Promote fvg_detector.py from a standalone module to a scored confluence input. Define OB + FVG overlap bonus and validate contribution to signal quality."
  },
  {
    id: "t7", col: "To Do", priority: "Medium",
    title: "Signal Source Evaluation: Sweep vs. Confluence",
    desc: "Sweep signals PF ~1.74 vs. confluence signals PF ~0.99. Determine whether confluence signals should be redesigned or removed. Data-driven decision required."
  },
  {
    id: "t8", col: "To Do", priority: "Low",
    title: "HAR-RV Volatility Forecasting Module",
    desc: "Implement Corsi (2009) HAR-RV model from 5-minute MNQ returns. Rolling daily/weekly/monthly RV components. Replace ATR-only stop sizing with model-informed dynamic sizing."
  },
  {
    id: "t9", col: "In Progress", priority: "High",
    title: "Paper Trading on Tradovate Demo",
    desc: "System is v2.0.0-rc1 — PAPER TRADING APPROVED. Deploy bot against Tradovate demo account. Monitor live PnL vs backtest baseline (PF 1.73, 61.9% WR, $4,264/month avg). Watch for fill slippage vs. calibrated model."
  },
  {
    id: "t9b", col: "In Progress", priority: "High",
    title: "IBKR Live Data Connector",
    desc: "Sub-modules partially built. Complete WebSocket feed integration, ensure process_bar() interface parity between historical CSV replay and live feed. Validate tick format match."
  },
  {
    id: "t10", col: "In Progress", priority: "High",
    title: "UCL v2 Routing Audit",
    desc: "UCL v2 added lower-quality trades that diluted metrics. Audit watch-state routing thresholds. Confirm signals ≥0.75 route to immediate entry, not watch state."
  },
  {
    id: "t11", col: "Done", priority: "High",
    title: "Causal Replay Engine",
    desc: "CausalReplayEngine / ReplaySimulator built. HTF bars constructed incrementally from 1-minute source data. Look-ahead bias eliminated."
  },
  {
    id: "t12", col: "Done", priority: "High",
    title: "UCL v2 Implementation",
    desc: "Score 0.60–0.74 → watch state (RECLAIM → FVG_FORM → FVG_TAP). Score ≥0.75 → immediate entry with FVG boost (+0.05). Wide-stop sweeps → tight-stop conversion."
  },
  {
    id: "t13", col: "Done", priority: "Medium",
    title: "607-Trade Profile Analysis",
    desc: "Fat-tail dependency confirmed: top 10% of trades = ~241% of profit. C2 runner = ~60% total PnL. Sweep PF ~1.74 vs. confluence PF ~0.99. HTF gate identified as highest-value filter."
  },
];

function genId() { return "t" + Date.now() + Math.random().toString(36).slice(2, 6); }

const STORAGE_KEY = "algo-kanban-v1";

export default function App() {
  const [tasks, setTasks] = useState(DEFAULT_TASKS);
  const [loaded, setLoaded] = useState(false);
  const [dragging, setDragging] = useState(null);
  const [dragOver, setDragOver] = useState(null);
  const [modal, setModal] = useState(null); // {mode:'add'|'edit', task, col}
  const [aiLoading, setAiLoading] = useState(false);
  const [aiSuggestions, setAiSuggestions] = useState([]);
  const [aiError, setAiError] = useState(null);
  const [saveStatus, setSaveStatus] = useState("saved");
  const [apiKey, setApiKey] = useState("");

  // Load from storage
  useEffect(() => {
    (async () => {
      try {
        const r = await window.storage.get(STORAGE_KEY);
        if (r && r.value) setTasks(JSON.parse(r.value));
      } catch (_) {}
      setLoaded(true);
    })();
  }, []);

  // Save to storage
  useEffect(() => {
    if (!loaded) return;
    setSaveStatus("saving");
    const t = setTimeout(async () => {
      try {
        await window.storage.set(STORAGE_KEY, JSON.stringify(tasks));
        setSaveStatus("saved");
      } catch (_) { setSaveStatus("error"); }
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

  const fetchAISuggestions = async () => {
    setAiLoading(true); setAiError(null); setAiSuggestions([]);
    const summary = COLS.map(c => {
      const ts = tasks.filter(t => t.col === c).map(t => `- [${t.priority}] ${t.title}`).join("\n");
      return `${c}:\n${ts || "  (empty)"}`;
    }).join("\n\n");
    try {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-api-key": apiKey, "anthropic-version": "2023-06-01" },
        body: JSON.stringify({
          model: "claude-sonnet-4-6",
          max_tokens: 1000,
          messages: [{
            role: "user",
            content: `You are an expert systematic trading system architect. Analyse this Kanban board for an institutional-grade NQ/MNQ futures algo trading bot and suggest the next 3 high-priority tasks that should be added or actioned. Be specific, technical, and concise. Respond ONLY with a JSON array of exactly 3 objects: [{\"title\": \"...\", \"priority\": \"High|Medium|Low\", \"desc\": \"...\"}]. No markdown, no preamble.\n\nBoard state:\n${summary}`
          }]
        })
      });
      const data = await res.json();
      const text = data.content?.find(b => b.type === "text")?.text || "[]";
      const clean = text.replace(/```json|```/g, "").trim();
      setAiSuggestions(JSON.parse(clean));
    } catch (e) {
      setAiError("AI suggestion failed. Check connectivity.");
    }
    setAiLoading(false);
  };

  const addSuggestion = (s) => {
    setTasks(prev => [...prev, { ...s, id: genId(), col: "To Do" }]);
    setAiSuggestions(prev => prev.filter(x => x !== s));
  };

  return (
    <div style={{ minHeight: "100vh", background: "#0a0e1a", color: "#e2e8f0", fontFamily: "'IBM Plex Mono', 'Courier New', monospace", padding: "24px 16px" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24, flexWrap: "wrap", gap: 12 }}>
        <div>
          <div style={{ fontSize: 11, color: "#3b82f6", letterSpacing: 3, textTransform: "uppercase", marginBottom: 4 }}>NQ BOT SYSTEM BUILD</div>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: "#f1f5f9" }}>Algo Trading Kanban</h1>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span style={{ fontSize: 10, color: saveStatus === "saved" ? "#22c55e" : saveStatus === "saving" ? "#f59e0b" : "#ef4444", letterSpacing: 1 }}>
            {saveStatus === "saved" ? "● SAVED" : saveStatus === "saving" ? "● SAVING..." : "● SAVE ERROR"}
          </span>
          <input value={apiKey} onChange={(e: { target: { value: string } }) => setApiKey(e.target.value)} placeholder="Anthropic API key..."
            type="password"
            style={{ background: "#111827", border: "1px solid #1e2a4a", color: "#f1f5f9", borderRadius: 6, padding: "7px 10px", fontSize: 11, fontFamily: "inherit", width: 180 }} />
          <button onClick={fetchAISuggestions} disabled={aiLoading || !apiKey}
            style={{ background: aiLoading ? "#1e2a4a" : "#1d4ed8", color: "#fff", border: "none", borderRadius: 6, padding: "8px 16px", fontSize: 12, cursor: aiLoading ? "not-allowed" : "pointer", letterSpacing: 1 }}>
            {aiLoading ? "ANALYSING..." : "⚡ AI SUGGEST"}
          </button>
        </div>
      </div>

      {/* AI Suggestions */}
      {(aiSuggestions.length > 0 || aiError) && (
        <div style={{ marginBottom: 20, background: "#0f172a", border: "1px solid #1d4ed8", borderRadius: 8, padding: 16 }}>
          <div style={{ fontSize: 11, color: "#3b82f6", letterSpacing: 2, marginBottom: 12 }}>AI RECOMMENDATIONS</div>
          {aiError && <div style={{ color: "#ef4444", fontSize: 12 }}>{aiError}</div>}
          {aiSuggestions.map((s, i) => (
            <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 12, padding: "10px 0", borderBottom: i < aiSuggestions.length - 1 ? "1px solid #1e2a4a" : "none" }}>
              <div style={{ flex: 1 }}>
                <span style={{ fontSize: 11, color: P_COLOR[s.priority], fontWeight: 700, marginRight: 8 }}>{s.priority.toUpperCase()}</span>
                <span style={{ fontSize: 13, color: "#f1f5f9" }}>{s.title}</span>
                <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>{s.desc}</div>
              </div>
              <button onClick={() => addSuggestion(s)}
                style={{ background: "#14532d", color: "#22c55e", border: "1px solid #22c55e", borderRadius: 4, padding: "4px 10px", fontSize: 11, cursor: "pointer", whiteSpace: "nowrap" }}>
                + ADD
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Columns */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
        {COLS.map(col => {
          const colTasks = tasks.filter(t => t.col === col);
          const isOver = dragOver === col;
          return (
            <div key={col}
              onDragOver={e => { e.preventDefault(); setDragOver(col); }}
              onDragLeave={() => setDragOver(null)}
              onDrop={() => handleDrop(col)}
              style={{ background: isOver ? "#0f1f3d" : "#0d1221", border: `1px solid ${isOver ? "#3b82f6" : "#1e2a4a"}`, borderRadius: 10, padding: 14, minHeight: 400, transition: "border-color 0.15s, background 0.15s" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
                <div>
                  <span style={{ fontSize: 11, letterSpacing: 2, color: col === "To Do" ? "#94a3b8" : col === "In Progress" ? "#3b82f6" : "#22c55e", textTransform: "uppercase", fontWeight: 700 }}>{col}</span>
                  <span style={{ marginLeft: 8, fontSize: 11, background: "#1e2a4a", color: "#94a3b8", borderRadius: 10, padding: "1px 7px" }}>{colTasks.length}</span>
                </div>
                <button onClick={() => setModal({ mode: "add", task: { id: genId(), col, priority: "Medium", title: "", desc: "" } })}
                  style={{ background: "none", border: "1px solid #1e2a4a", color: "#94a3b8", borderRadius: 4, width: 24, height: 24, cursor: "pointer", fontSize: 16, lineHeight: "1", display: "flex", alignItems: "center", justifyContent: "center" }}>+</button>
              </div>
              {colTasks.map(task => (
                <div key={task.id} draggable
                  onDragStart={() => setDragging(task.id)}
                  onDragEnd={() => { setDragging(null); setDragOver(null); }}
                  onClick={() => setModal({ mode: "edit", task: { ...task } })}
                  style={{ background: "#111827", border: "1px solid #1e2a4a", borderLeft: `3px solid ${P_COLOR[task.priority]}`, borderRadius: 6, padding: "10px 12px", marginBottom: 10, cursor: "grab", transition: "border-color 0.15s", userSelect: "none" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: "#f1f5f9", lineHeight: 1.4, flex: 1 }}>{task.title}</div>
                    <span style={{ fontSize: 9, color: P_COLOR[task.priority], border: `1px solid ${P_COLOR[task.priority]}`, borderRadius: 3, padding: "1px 5px", whiteSpace: "nowrap", letterSpacing: 1 }}>{task.priority.toUpperCase()}</span>
                  </div>
                  {task.desc && <div style={{ fontSize: 10, color: "#64748b", marginTop: 6, lineHeight: 1.5, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{task.desc}</div>}
                </div>
              ))}
            </div>
          );
        })}
      </div>

      {/* Modal */}
      {modal && <TaskModal modal={modal} onSave={saveTask} onDelete={deleteTask} onClose={() => setModal(null)} />}
    </div>
  );
}

function TaskModal({ modal, onSave, onDelete, onClose }) {
  const [task, setTask] = useState(modal.task);
  const set = (k, v) => setTask(p => ({ ...p, [k]: v }));

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.75)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100, padding: 16 }}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{ background: "#0d1221", border: "1px solid #1e2a4a", borderRadius: 10, padding: 24, width: "100%", maxWidth: 520 }}>
        <div style={{ fontSize: 11, color: "#3b82f6", letterSpacing: 2, marginBottom: 16 }}>{modal.mode === "add" ? "NEW TASK" : "EDIT TASK"}</div>
        <input value={task.title} onChange={e => set("title", e.target.value)} placeholder="Task title..."
          style={{ width: "100%", background: "#111827", border: "1px solid #1e2a4a", color: "#f1f5f9", borderRadius: 6, padding: "8px 12px", fontSize: 13, marginBottom: 12, boxSizing: "border-box", fontFamily: "inherit" }} />
        <textarea value={task.desc} onChange={e => set("desc", e.target.value)} placeholder="Description..." rows={4}
          style={{ width: "100%", background: "#111827", border: "1px solid #1e2a4a", color: "#f1f5f9", borderRadius: 6, padding: "8px 12px", fontSize: 12, marginBottom: 12, boxSizing: "border-box", fontFamily: "inherit", resize: "vertical" }} />
        <div style={{ display: "flex", gap: 10, marginBottom: 20, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 120 }}>
            <div style={{ fontSize: 10, color: "#64748b", letterSpacing: 1, marginBottom: 6 }}>PRIORITY</div>
            <select value={task.priority} onChange={e => set("priority", e.target.value)}
              style={{ width: "100%", background: "#111827", border: "1px solid #1e2a4a", color: "#f1f5f9", borderRadius: 6, padding: "7px 10px", fontSize: 12, fontFamily: "inherit" }}>
              {PRIORITIES.map(p => <option key={p}>{p}</option>)}
            </select>
          </div>
          <div style={{ flex: 1, minWidth: 120 }}>
            <div style={{ fontSize: 10, color: "#64748b", letterSpacing: 1, marginBottom: 6 }}>COLUMN</div>
            <select value={task.col} onChange={e => set("col", e.target.value)}
              style={{ width: "100%", background: "#111827", border: "1px solid #1e2a4a", color: "#f1f5f9", borderRadius: 6, padding: "7px 10px", fontSize: 12, fontFamily: "inherit" }}>
              {COLS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          {modal.mode === "edit"
            ? <button onClick={() => onDelete(task.id)} style={{ background: "none", border: "1px solid #ef4444", color: "#ef4444", borderRadius: 6, padding: "8px 16px", fontSize: 12, cursor: "pointer", fontFamily: "inherit" }}>DELETE</button>
            : <div />}
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={onClose} style={{ background: "none", border: "1px solid #1e2a4a", color: "#94a3b8", borderRadius: 6, padding: "8px 16px", fontSize: 12, cursor: "pointer", fontFamily: "inherit" }}>CANCEL</button>
            <button onClick={() => onSave(task)} disabled={!task.title.trim()}
              style={{ background: task.title.trim() ? "#1d4ed8" : "#1e2a4a", color: "#fff", border: "none", borderRadius: 6, padding: "8px 16px", fontSize: 12, cursor: task.title.trim() ? "pointer" : "not-allowed", fontFamily: "inherit" }}>SAVE</button>
          </div>
        </div>
      </div>
    </div>
  );
}
