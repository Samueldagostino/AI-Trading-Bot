"""
Paper Trading Live Monitor
============================
Reads logs/paper_trades.json and logs/paper_decisions.json in real-time,
renders a live terminal dashboard showing:

  - Current position (direction, entry, unrealized P&L)
  - Today's stats (trades, PnL, WR, blocked signals)
  - HC filter and HTF block counts
  - Session totals vs OOS baseline
  - Anomaly alerts (rejections, connection issues, out-of-hours)

Usage:
    python scripts/paper_monitor.py              # Live tail mode
    python scripts/paper_monitor.py --snapshot    # One-time snapshot

Reads from:
    logs/paper_trades.json
    logs/paper_decisions.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
LOGS_DIR = project_dir / "logs"
TRADES_LOG = LOGS_DIR / "paper_trades.json"
DECISIONS_LOG = LOGS_DIR / "paper_decisions.json"

# OOS baseline
OOS_EXPECTANCY = 7.72
OOS_WIN_RATE = 46.7
OOS_PF = 1.15
OOS_TRADES_PER_MONTH = 125

# Safety limits
DAILY_LOSS_LIMIT = 500.0
MAX_CONTRACTS = 2
MAX_STOP_PTS = 30.0


def load_json(path: Path) -> List[Dict]:
    """Load a JSON log file, returning empty list if not found."""
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp string."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class MonitorState:
    """Computed state from log files."""

    def __init__(self):
        self.trades_log: List[Dict] = []
        self.decisions_log: List[Dict] = []

        # Current position
        self.has_position = False
        self.position_direction = ""
        self.position_entry_price = 0.0
        self.position_stop = 0.0
        self.position_c1_target = 0.0
        self.position_entry_time = ""
        self.position_score = 0.0
        self.position_regime = ""

        # Today's stats
        self.today_date = ""
        self.today_trades = 0
        self.today_pnl = 0.0
        self.today_wins = 0
        self.today_losses = 0
        self.today_blocked = 0
        self.today_entries = 0
        self.today_hc_blocks = 0
        self.today_htf_blocks = 0

        # Session totals
        self.total_trades = 0
        self.total_pnl = 0.0
        self.total_wins = 0
        self.total_losses = 0
        self.c1_pnl = 0.0
        self.c2_pnl = 0.0

        # Connection
        self.connected = False
        self.last_data_time = ""
        self.is_halted = False
        self.halt_reason = ""
        self.daily_loss_limit_hit = False

        # Anomalies
        self.anomalies: List[Dict] = []

    def compute(self) -> None:
        """Compute state from raw logs."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.today_date = today

        # Process trades log
        for event in self.trades_log:
            ts = event.get("timestamp", "")
            evt = event.get("event", "")
            event_date = ts[:10] if ts else ""

            if evt == "connection":
                self.connected = event.get("status") == "connected"
            elif evt == "connection_warning":
                self._add_anomaly("CONNECTION", f"WS issue: md={event.get('md_ws')}, order={event.get('order_ws')}", ts)
            elif evt == "connection_restored":
                self.connected = True
            elif evt == "emergency_flatten":
                self._add_anomaly("EMERGENCY", f"Flatten: {event.get('reason', '?')}", ts)
                self.is_halted = True
                self.halt_reason = event.get("reason", "")
            elif evt == "daily_loss_limit":
                self.daily_loss_limit_hit = True
                self._add_anomaly("LOSS LIMIT", f"Daily PnL: ${event.get('daily_pnl', 0):.2f}", ts)
            elif evt == "fill":
                self.last_data_time = ts
            elif evt == "trade_closed":
                pnl = event.get("pnl", 0)
                self.total_trades += 1
                self.total_pnl += pnl
                if pnl > 0:
                    self.total_wins += 1
                elif pnl < 0:
                    self.total_losses += 1

                if event_date == today:
                    self.today_trades += 1
                    self.today_pnl += pnl
                    if pnl > 0:
                        self.today_wins += 1
                    else:
                        self.today_losses += 1
            elif evt == "entry_attempt":
                stop = event.get("c2_initial_stop", 0)
                entry = event.get("entry_price", 0)
                if stop and entry and abs(entry - stop) > MAX_STOP_PTS + 1:
                    self._add_anomaly("STOP>30", f"Stop distance: {abs(entry - stop):.1f}pts", ts)
            elif evt == "blocked":
                reason = event.get("reason", "")
                if event_date == today:
                    self.today_blocked += 1

        # Process decisions log
        for decision in self.decisions_log:
            ts = decision.get("timestamp", "")
            dec = decision.get("decision", "")
            event_date = ts[:10] if ts else ""

            if dec == "entry":
                self.has_position = True
                self.position_direction = decision.get("direction", "")
                self.position_entry_price = decision.get("entry_price", 0)
                self.position_stop = decision.get("stop", 0)
                self.position_c1_target = decision.get("c1_target", 0)
                self.position_entry_time = ts
                self.position_score = decision.get("signal_score", 0)
                self.position_regime = decision.get("regime", "")
                if event_date == today:
                    self.today_entries += 1

            elif dec == "trade_closed":
                self.has_position = False
                pnl = decision.get("total_pnl", 0)
                c1 = decision.get("c1_pnl", 0)
                c2 = decision.get("c2_pnl", 0)
                self.c1_pnl += c1
                self.c2_pnl += c2

            elif dec == "bar_skipped":
                reason = decision.get("reason", "")
                if reason == "outside_session" and event_date == today:
                    pass  # Normal, don't count
                elif reason == "daily_loss_limit":
                    self.daily_loss_limit_hit = True

            elif dec == "session_flatten":
                self.has_position = False

            elif dec == "session_start":
                self.last_data_time = ts

    def _add_anomaly(self, category: str, message: str, timestamp: str) -> None:
        self.anomalies.append({
            "time": timestamp,
            "category": category,
            "message": message,
        })


def render_dashboard(state: MonitorState) -> str:
    """Render the terminal dashboard as a string."""
    lines = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines.append("")
    lines.append(f"  PAPER TRADING MONITOR — Config D")
    lines.append(f"  {now}")
    lines.append(f"  {'=' * 56}")

    # Status bar
    conn_str = "CONNECTED" if state.connected else "DISCONNECTED"
    halt_str = f" | HALTED: {state.halt_reason}" if state.is_halted else ""
    loss_str = " | DAILY LOSS LIMIT HIT" if state.daily_loss_limit_hit else ""
    lines.append(f"  Status: {conn_str}{halt_str}{loss_str}")
    lines.append("")

    # Current position
    lines.append(f"  CURRENT POSITION")
    lines.append(f"  {'─' * 56}")
    if state.has_position:
        direction = state.position_direction.upper()
        entry = state.position_entry_price
        stop = state.position_stop
        target = state.position_c1_target
        stop_dist = abs(entry - stop) if entry and stop else 0
        target_dist = abs(target - entry) if target and entry else 0

        lines.append(f"  Direction:  {direction}")
        lines.append(f"  Entry:      {entry:.2f}")
        lines.append(f"  Stop:       {stop:.2f}  ({stop_dist:.1f}pts)")
        lines.append(f"  C1 Target:  {target:.2f}  ({target_dist:.1f}pts)")
        lines.append(f"  Score:      {state.position_score:.3f}")
        lines.append(f"  Regime:     {state.position_regime}")
        lines.append(f"  Since:      {state.position_entry_time[:19]}")
    else:
        lines.append(f"  FLAT — no open position")
    lines.append("")

    # Today's stats
    lines.append(f"  TODAY ({state.today_date})")
    lines.append(f"  {'─' * 56}")
    today_wr = (state.today_wins / state.today_trades * 100) if state.today_trades > 0 else 0
    today_exp = (state.today_pnl / state.today_trades) if state.today_trades > 0 else 0
    lines.append(f"  Trades:       {state.today_trades}")
    lines.append(f"  PnL:          ${state.today_pnl:+.2f}  (limit: -${DAILY_LOSS_LIMIT:.0f})")
    lines.append(f"  Win Rate:     {today_wr:.1f}%  ({state.today_wins}W / {state.today_losses}L)")
    lines.append(f"  Expectancy:   ${today_exp:+.2f}  (OOS baseline: ${OOS_EXPECTANCY:.2f})")
    lines.append(f"  Blocked:      {state.today_blocked}")
    lines.append("")

    # Session totals
    lines.append(f"  SESSION TOTALS")
    lines.append(f"  {'─' * 56}")
    total_wr = (state.total_wins / state.total_trades * 100) if state.total_trades > 0 else 0
    total_exp = (state.total_pnl / state.total_trades) if state.total_trades > 0 else 0
    gross_profit = 0
    gross_loss = 0
    # Approximate PF from WR and exp
    if state.total_wins > 0 and state.total_losses > 0 and state.total_pnl != 0:
        # Use C1+C2 split
        pf_str = f"~{abs(state.c1_pnl + state.c2_pnl) / max(abs(state.total_pnl - (state.c1_pnl + state.c2_pnl)), 0.01):.2f}" if state.total_losses > 0 else "inf"
    else:
        pf_str = "—"

    lines.append(f"  Total Trades: {state.total_trades}")
    lines.append(f"  Total PnL:    ${state.total_pnl:+.2f}")
    lines.append(f"  Win Rate:     {total_wr:.1f}%  (OOS: {OOS_WIN_RATE}%)")
    lines.append(f"  Expectancy:   ${total_exp:+.2f}  (OOS: ${OOS_EXPECTANCY:.2f})")
    lines.append(f"  C1 PnL:       ${state.c1_pnl:+.2f}")
    lines.append(f"  C2 PnL:       ${state.c2_pnl:+.2f}")
    lines.append("")

    # Anomalies
    recent_anomalies = state.anomalies[-10:]  # Last 10
    lines.append(f"  ANOMALIES ({len(state.anomalies)} total)")
    lines.append(f"  {'─' * 56}")
    if recent_anomalies:
        for a in recent_anomalies:
            t = a["time"][:19] if a.get("time") else "?"
            lines.append(f"  [{a['category']:>12}] {t} — {a['message']}")
    else:
        lines.append(f"  None")
    lines.append("")
    lines.append(f"  {'=' * 56}")

    return "\n".join(lines)


def run_snapshot():
    """One-time snapshot of current state."""
    trades = load_json(TRADES_LOG)
    decisions = load_json(DECISIONS_LOG)

    state = MonitorState()
    state.trades_log = trades
    state.decisions_log = decisions
    state.compute()

    print(render_dashboard(state))


def run_live():
    """Live tail mode — refreshes every 2 seconds."""
    print("Paper Trading Monitor — Live Mode (Ctrl+C to exit)")
    print(f"Watching: {TRADES_LOG}")
    print(f"          {DECISIONS_LOG}")

    trades_mtime = 0
    decisions_mtime = 0

    try:
        while True:
            # Check if files changed
            t_mtime = TRADES_LOG.stat().st_mtime if TRADES_LOG.exists() else 0
            d_mtime = DECISIONS_LOG.stat().st_mtime if DECISIONS_LOG.exists() else 0

            if t_mtime != trades_mtime or d_mtime != decisions_mtime:
                trades_mtime = t_mtime
                decisions_mtime = d_mtime

                trades = load_json(TRADES_LOG)
                decisions = load_json(DECISIONS_LOG)

                state = MonitorState()
                state.trades_log = trades
                state.decisions_log = decisions
                state.compute()

                # Clear screen and render
                os.system("clear" if os.name != "nt" else "cls")
                print(render_dashboard(state))

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Live Monitor")
    parser.add_argument(
        "--snapshot", action="store_true",
        help="One-time snapshot instead of live tail"
    )
    args = parser.parse_args()

    if args.snapshot:
        run_snapshot()
    else:
        run_live()


if __name__ == "__main__":
    main()
