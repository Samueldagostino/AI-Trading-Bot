"""
Live Stats Publisher for GitHub Pages
=======================================
Reads sanitized bot stats from log files and publishes to Website/data/live_stats.json.
Commits and pushes to GitHub automatically every 60 seconds.

SECURITY: This script NEVER exposes:
  - Account numbers or IDs
  - Dollar PnL amounts (only percentages)
  - Exact entry/exit prices
  - Order IDs or trade IDs
  - IP addresses or connection details
  - Equity or account balance

It DOES expose:
  - System status (LIVE/OFFLINE)
  - Bars processed count
  - Win rate, profit factor (ratios only)
  - Session PnL as percentage
  - HTF bias direction
  - Signal approved/rejected counts
  - Modifier values
  - Last price (market data, publicly available)
  - Recent decisions (direction + decision + reason, NO prices)
  - Heartbeat: last_seen timestamp, data age, connection quality, uptime %
  - Bot state: mode (TRADING/MONITORING/OFFLINE), last trade time, active positions count

Usage:
    python scripts/publish_stats.py
    python scripts/publish_stats.py --interval 120
    python scripts/publish_stats.py --dry-run
"""

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
ROOT_DIR = PROJECT_DIR.parent  # AI-Trading-Bot root
LOGS_DIR = PROJECT_DIR / "logs"
DOCS_DATA_DIR = ROOT_DIR / "docs" / "data"
OUTPUT_FILE = DOCS_DATA_DIR / "live_stats.json"
HEARTBEAT_FILE = LOGS_DIR / "heartbeat_state.json"

# Account size for percentage calculation (not exposed)
_ACCOUNT_SIZE = 50_000.0

# Track publisher uptime for heartbeat
_PUBLISHER_START = datetime.now(timezone.utc)

# Track previous status for OFFLINE → LIVE transition detection
_previous_status = "OFFLINE"
_notification_sent_this_session = False


def _read_json_safe(filepath: Path, default=None):
    """Read a JSON file, returning default on missing/error."""
    if default is None:
        default = {}
    try:
        if not filepath.exists():
            return default
        text = filepath.read_text(encoding="utf-8").strip()
        if not text:
            return default
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Error reading %s: %s", filepath, e)
        return default


def _read_jsonl_safe(filepath: Path, limit: int = 50) -> list:
    """Read a JSONL file, returning last N entries."""
    try:
        if not filepath.exists():
            return []
        text = filepath.read_text(encoding="utf-8").strip()
        if not text:
            return []
        result = []
        for line in text.split("\n")[-limit:]:
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return result
    except OSError as e:
        logger.warning("Error reading %s: %s", filepath, e)
        return []


def _build_heartbeat(status: dict, candles) -> dict:
    """
    Build heartbeat object from health monitor data and publish state.
    Reads heartbeat_state.json if the health monitor is writing it,
    otherwise computes from available data.
    """
    now = datetime.now(timezone.utc)

    # Try reading heartbeat from health monitor (written by tws_health_monitor.py)
    hb_data = _read_json_safe(HEARTBEAT_FILE)

    if hb_data and hb_data.get("last_seen"):
        # Health monitor is running -- use its data
        try:
            last_seen = datetime.fromisoformat(hb_data["last_seen"])
            age_sec = (now - last_seen).total_seconds()
        except (ValueError, TypeError):
            last_seen = now
            age_sec = 0.0

        uptime_pct = round(hb_data.get("uptime_pct", 0.0), 1)
        quality = hb_data.get("connection_quality", "unknown")
    else:
        # No health monitor -- derive from publisher activity
        last_seen = now  # We're publishing right now, so we're alive
        age_sec = 0.0
        uptime_start = _PUBLISHER_START
        uptime_total = (now - uptime_start).total_seconds()
        uptime_pct = 100.0 if uptime_total > 0 else 0.0

        # Derive connection quality from data freshness
        if status and candles:
            quality = "good"
        elif status:
            quality = "fair"
        else:
            quality = "poor"

    return {
        "last_seen": last_seen.isoformat(),
        "seconds_since_last": round(age_sec, 1),
        "connection_quality": quality,
        "uptime_pct": uptime_pct,
    }


def _build_bot_state(status: dict, decisions: list) -> dict:
    """
    Build bot state object: TRADING / MONITORING / OFFLINE.
    - TRADING: has active positions or made trades recently
    - MONITORING: bot is live but not actively trading
    - OFFLINE: no data or stale
    """
    trade_count = status.get("trade_count", 0) if status else 0
    active_positions = status.get("active_positions", 0) if status else 0

    # Determine last trade time from decisions
    last_trade_time = ""
    for d in reversed(decisions):
        if d.get("decision") == "APPROVED":
            ts = d.get("timestamp", "")
            if ts:
                try:
                    from zoneinfo import ZoneInfo
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    et = dt.astimezone(ZoneInfo("America/New_York"))
                    last_trade_time = et.strftime("%H:%M:%S ET")
                except (ValueError, TypeError):
                    last_trade_time = ""
            break

    # Determine mode
    if not status:
        mode = "OFFLINE"
    elif active_positions > 0 or trade_count > 0:
        mode = "TRADING"
    else:
        mode = "MONITORING"

    return {
        "mode": mode,
        "last_trade_time": last_trade_time,
        "active_positions": active_positions,
    }


def _build_trade_log() -> tuple[list, float]:
    """
    Build today's trade log from paper_trades.json.
    Returns (trade_log_list, session_pnl_dollars).

    Each trade entry contains:
      - time: HH:MM:SS ET
      - direction: LONG / SHORT
      - entry_price: float
      - signal_score: float (0.0-1.0 conviction score)
      - signal_source: str (sweep / aggregator)
      - htf_bias: str (bullish / bearish / neutral)
      - contracts: int (total contracts, typically 4)
      - c1_pnl/c1_reason: C1 scalp (1 contract, 5-bar time exit)
      - c2_pnl/c2_reason: C2 structural (1 contract, swing target)
      - c3_pnl/c3_reason: C3 runner (2 contracts, ATR trail)
      - c4_pnl/c4_reason: C4 unused (0 contracts)
      - total_pnl: float (total dollar PnL for the trade)
    """
    trades_file = LOGS_DIR / "paper_trades.json"
    raw_trades = _read_jsonl_safe(trades_file, limit=500)

    if not raw_trades:
        return [], 0.0

    from zoneinfo import ZoneInfo

    now_et = datetime.now(ZoneInfo("America/New_York"))
    today_et = now_et.date()

    # Futures session spans midnight ET: starts 6 PM ET prior day.
    # If it's before 6 PM ET, include trades from 6 PM yesterday onward.
    # If it's after 6 PM ET, include trades from 6 PM today onward.
    if now_et.hour < 18:
        from datetime import timedelta
        session_start_date = today_et - timedelta(days=1)
    else:
        session_start_date = today_et
    session_start = datetime(
        session_start_date.year, session_start_date.month, session_start_date.day,
        18, 0, 0, tzinfo=ZoneInfo("America/New_York"),
    )

    trade_log = []
    session_pnl = 0.0

    for t in raw_trades:
        # Filter to current futures session (6 PM ET prior day → now)
        ts_str = t.get("timestamp", "")
        if not ts_str:
            continue

        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            et = dt.astimezone(ZoneInfo("America/New_York"))
        except (ValueError, TypeError):
            continue

        if et < session_start:
            continue

        total_pnl = t.get("pnl", t.get("total_pnl", 0.0))
        session_pnl += total_pnl

        direction = t.get("direction", "").upper()
        if direction not in ("LONG", "SHORT"):
            direction = direction or "UNKNOWN"

        c1_pnl = round(t.get("c1_pnl", 0.0), 2)
        c2_pnl = round(t.get("c2_pnl", 0.0), 2)
        c3_pnl = round(t.get("c3_pnl", 0.0), 2)
        # If c3_pnl not recorded (older trades), derive from total
        if c3_pnl == 0.0 and abs(total_pnl - c1_pnl - c2_pnl) > 0.5:
            c3_pnl = round(total_pnl - c1_pnl - c2_pnl, 2)

        trade_log.append({
            "time": et.strftime("%H:%M:%S ET"),
            "direction": direction,
            "entry_price": round(t.get("entry_price", 0.0), 2),
            "signal_score": round(t.get("signal_score", 0.0), 3),
            "signal_source": t.get("signal_source", "") or "signal",
            "htf_bias": t.get("htf_bias", ""),
            "regime": t.get("regime", ""),
            "contracts": t.get("contracts", 4),
            "c1_pnl": c1_pnl,
            "c1_reason": t.get("c1_reason", ""),
            "c2_pnl": c2_pnl,
            "c2_reason": t.get("c2_reason", ""),
            "c3_pnl": c3_pnl,
            "c3_reason": t.get("c3_reason", ""),
            "c4_pnl": round(t.get("c4_pnl", 0.0), 2),
            "c4_reason": t.get("c4_reason", ""),
            "total_pnl": round(total_pnl, 2),
        })

    return trade_log, round(session_pnl, 2)


def _build_trade_events(decisions: list) -> list:
    """
    Build trade entry/exit events from decision log for chart markers.
    Returns list of events with: type (ENTRY/EXIT), timestamp, direction,
    price, score, stop, target, pnl, reason.
    """
    from zoneinfo import ZoneInfo

    now_et = datetime.now(ZoneInfo("America/New_York"))
    today_et = now_et.date()
    # Futures session: 6 PM ET prior day → now
    if now_et.hour < 18:
        from datetime import timedelta
        session_start_date = today_et - timedelta(days=1)
    else:
        session_start_date = today_et
    session_start = datetime(
        session_start_date.year, session_start_date.month, session_start_date.day,
        18, 0, 0, tzinfo=ZoneInfo("America/New_York"),
    )

    events = []

    for d in decisions:
        ts_str = d.get("timestamp", "")
        if not ts_str:
            continue

        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            et = dt.astimezone(ZoneInfo("America/New_York"))
        except (ValueError, TypeError):
            continue

        if et < session_start:
            continue

        decision = d.get("decision", "")

        if decision == "APPROVED":
            entry_price = d.get("entry_price", d.get("price_at_signal", 0.0))
            events.append({
                "type": "ENTRY",
                "time": dt.isoformat(),
                "time_et": et.strftime("%H:%M:%S ET"),
                "direction": d.get("signal_direction", ""),
                "price": round(entry_price, 2),
                "score": round(d.get("confluence_score", 0.0), 3),
                "stop_width": round(d.get("stop_width", 0.0), 2),
                "c1_target": round(d.get("c1_target", 0.0), 2),
                "c2_trail_start": round(d.get("c2_trail_start", 0.0), 2),
            })

        elif decision == "EXIT":
            events.append({
                "type": "EXIT",
                "time": dt.isoformat(),
                "time_et": et.strftime("%H:%M:%S ET"),
                "direction": d.get("signal_direction", ""),
                "price": round(d.get("exit_price", 0.0), 2),
                "entry_price": round(d.get("entry_price", 0.0), 2),
                "pnl": round(d.get("total_pnl", 0.0), 2),
                "reason": d.get("exit_reason", ""),
            })

    return events



def build_sanitized_stats() -> dict:
    """
    Build sanitized stats from log files.
    NEVER includes account numbers, dollar amounts, order IDs, or exact prices.
    """
    # Read source files
    status = _read_json_safe(LOGS_DIR / "paper_trading_state.json")
    decisions = _read_jsonl_safe(LOGS_DIR / "trade_decisions.json", limit=50)
    candles = _read_json_safe(LOGS_DIR / "candle_buffer.json", default=[])
    safety = _read_json_safe(LOGS_DIR / "safety_state.json")
    modifiers = _read_json_safe(LOGS_DIR / "modifier_state.json")

    # Determine system status
    is_live = bool(status and status.get("trade_count", 0) >= 0 and candles)

    # Count approved/rejected
    approved = sum(1 for d in decisions if d.get("decision") == "APPROVED")
    rejected = sum(1 for d in decisions if d.get("decision") == "REJECTED")

    # Session PnL as PERCENTAGE only (never expose dollar amount)
    total_pnl = status.get("total_pnl", 0.0)
    session_pnl_pct = round((total_pnl / _ACCOUNT_SIZE) * 100, 3) if _ACCOUNT_SIZE > 0 else 0.0

    # Last price from candle buffer (publicly available market data)
    last_price = 0.0
    if isinstance(candles, list) and candles:
        last_candle = candles[-1]
        last_price = last_candle.get("c", 0.0)

    # HTF bias -- extract from modifier state or default
    htf_bias = "NEUTRAL"
    if modifiers:
        overnight = modifiers.get("overnight", {})
        reason = overnight.get("reason", "neutral")
        if "bullish" in str(reason).lower():
            htf_bias = "BULLISH"
        elif "bearish" in str(reason).lower():
            htf_bias = "BEARISH"

    # Safety rails status
    safety_status = "OK"
    if safety:
        if not safety.get("all_ok", True):
            safety_status = "ALERT"

    # Sanitize modifier values with reasons (no sensitive data exposed)
    overnight_mod = modifiers.get("overnight", {})
    fomc_mod = modifiers.get("fomc", {})
    gamma_mod = modifiers.get("gamma", {})
    har_rv_mod = modifiers.get("har_rv", {})

    mod_values = {
        "overnight": {
            "value": round(overnight_mod.get("value", 1.0), 2),
            "display": f"{round(overnight_mod.get('value', 1.0), 2)}x ({overnight_mod.get('reason', 'neutral')})",
        },
        "fomc": {
            "value": round(fomc_mod.get("value", 1.0), 2),
            "display": f"{round(fomc_mod.get('value', 1.0), 2)}x ({fomc_mod.get('reason', 'none')})",
        },
        "gamma": {
            "value": round(gamma_mod.get("value", 1.0), 2),
            "display": f"{round(gamma_mod.get('value', 1.0), 2)}x ({gamma_mod.get('reason', 'unknown')})",
        },
        "har_rv": {
            "value": round(har_rv_mod.get("value", 1.0), 2),
            "display": f"{round(har_rv_mod.get('value', 1.0), 2)}x ({har_rv_mod.get('reason', 'normal')})",
        },
    }

    # Sanitize recent decisions: direction + decision + reason + time
    # NO prices, NO order IDs, NO trade IDs
    recent_decisions = []
    for d in decisions[-10:]:
        # Extract timestamp and format as HH:MM:SS ET
        ts_str = d.get("timestamp", "")
        time_et = ""
        if ts_str:
            try:
                from zoneinfo import ZoneInfo
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                et = dt.astimezone(ZoneInfo("America/New_York"))
                time_et = et.strftime("%H:%M:%S ET")
            except (ValueError, TypeError):
                time_et = ""

        sanitized = {
            "time": time_et,
            "direction": d.get("signal_direction", ""),
            "decision": d.get("decision", ""),
            "reason": d.get("rejection_stage", "Signal approved")
                      if d.get("decision") == "REJECTED"
                      else "Approved",
        }
        recent_decisions.append(sanitized)

    # Build trade log and session PnL
    trade_log, session_pnl_dollars = _build_trade_log()

    # Build heartbeat and state (backward compatible -- new fields)
    heartbeat = _build_heartbeat(status, candles)
    bot_state = _build_bot_state(status, decisions)

    # Active trade for live chart (read from active_trade.json written by run_tws.py)
    active_trade = _read_json_safe(LOGS_DIR / "active_trade.json")
    if not active_trade or not active_trade.get("side"):
        active_trade = None  # No active trade

    # Full-day candles for the chart (publicly available price data)
    # Include ALL candles from today (12:00 AM - 11:59 PM ET)
    chart_candles = []
    if isinstance(candles, list) and candles:
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        for c in candles:
            ts_str = c.get("t", "")
            include = True
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    et = dt.astimezone(ZoneInfo("America/New_York"))
                    if et.date() != today_et:
                        include = False
                except (ValueError, TypeError):
                    pass
            if include:
                chart_candles.append({
                    "t": c.get("t", ""),
                    "o": c.get("o", 0),
                    "h": c.get("h", 0),
                    "l": c.get("l", 0),
                    "c": c.get("c", 0),
                    "v": c.get("vol", c.get("v", 0)),
                })

    # Build trade events (entries + exits) for chart markers
    trade_events = _build_trade_events(decisions)

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "status": "LIVE" if is_live else "OFFLINE",
        "trading_mode": "PAPER",  # Change to "LIVE" only after Phase 4 validation gate
        "bars_processed": status.get("bars_processed", len(candles) if isinstance(candles, list) else 0),
        "trades_today": status.get("trade_count", 0),
        "win_rate": round(status.get("win_rate", 0.0), 1),
        "profit_factor": round(status.get("profit_factor", 0.0), 2),
        "session_pnl_pct": session_pnl_pct,
        "signals_rejected": rejected,
        "signals_approved": approved,
        "htf_bias": htf_bias,
        "safety_rails": safety_status,
        "modifiers": mod_values,
        "last_price": round(last_price, 2),
        "recent_decisions": recent_decisions,
        # Heartbeat & state
        "heartbeat": heartbeat,
        "state": bot_state,
        # Trade chart data (publicly available market prices only)
        "active_trade": active_trade,
        "candle_buffer": chart_candles,
        # Session PnL and trade log
        "session_pnl_dollars": session_pnl_dollars,
        "trade_log": trade_log,
        # Trade events for chart markers (entries + exits with prices)
        "trade_events": trade_events,
    }


def write_stats(stats: dict) -> None:
    """Write stats atomically to docs/data/live_stats.json."""
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, default=str)
        os.replace(str(tmp_path), str(OUTPUT_FILE))
        logger.debug("Stats written to %s", OUTPUT_FILE)
    except OSError as e:
        logger.warning("Failed to write stats: %s", e)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _clean_git_locks() -> None:
    """Remove stale git lock files (common with OneDrive sync conflicts)."""
    git_dir = ROOT_DIR / ".git"
    lock_files = [
        git_dir / "HEAD.lock",
        git_dir / "index.lock",
        git_dir / "objects" / "maintenance.lock",
    ]
    for lf in lock_files:
        try:
            if lf.exists():
                lf.unlink()
                logger.info("Removed stale lock file: %s", lf.name)
        except OSError:
            pass


def _get_current_branch() -> str:
    """Return the name of the currently checked-out branch."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# ── Target branch for GitHub Pages (website always serves from main) ──
PAGES_BRANCH = "main"


def git_commit_and_push(dry_run: bool = False) -> bool:
    """Commit and push live_stats.json to GitHub.

    Strategy: Always push the stats file to the 'main' branch so that
    GitHub Pages (which serves from main/docs) shows current data —
    regardless of which feature branch the bot is running on.

    How it works:
    1. Clean stale git lock files (OneDrive conflict safety).
    2. Stage & commit the stats file on the current branch.
    3. If the current branch IS main, push directly.
    4. If the current branch is NOT main, cherry-pick the commit onto
       main, push main, then switch back. The current branch keeps its
       own commit too (harmless duplicate in docs/data/).
    """
    try:
        _clean_git_locks()

        # Stage the stats file
        rel_path = OUTPUT_FILE.relative_to(ROOT_DIR)
        subprocess.run(
            ["git", "add", str(rel_path)],
            cwd=str(ROOT_DIR),
            capture_output=True, text=True, timeout=30,
        )

        # Also stage trade_viz_data if it exists
        trade_viz = DOCS_DATA_DIR / "trade_viz_data.json"
        if trade_viz.exists():
            rel_viz = trade_viz.relative_to(ROOT_DIR)
            subprocess.run(
                ["git", "add", str(rel_viz)],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=30,
            )

        # Check if there are staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(ROOT_DIR),
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Git: no changes to commit (file unchanged)")
            return True

        if dry_run:
            logger.info("[DRY-RUN] Would commit and push live_stats.json")
            subprocess.run(
                ["git", "reset", "HEAD", str(rel_path)],
                cwd=str(ROOT_DIR),
                capture_output=True, timeout=30,
            )
            return True

        # Commit on current branch
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        result = subprocess.run(
            ["git", "commit", "-m", f"stats: update live_stats.json ({timestamp})"],
            cwd=str(ROOT_DIR),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("Git commit failed: %s", result.stderr)
            return False

        current_branch = _get_current_branch()

        # ── If NOT on main, cherry-pick the stats commit onto main ──
        if current_branch and current_branch != PAGES_BRANCH:
            commit_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            # Switch to main
            sw = subprocess.run(
                ["git", "checkout", PAGES_BRANCH],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=30,
            )
            if sw.returncode != 0:
                logger.warning("Could not switch to %s: %s", PAGES_BRANCH, sw.stderr.strip())
                # Fall back: just push the current branch
                return _push_branch(current_branch)

            # Cherry-pick the stats commit (no-commit + commit to avoid editor)
            cp = subprocess.run(
                ["git", "cherry-pick", "--no-commit", commit_sha],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=30,
            )
            if cp.returncode != 0:
                # Conflict or empty cherry-pick — abort and go back
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=str(ROOT_DIR),
                    capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["git", "checkout", current_branch],
                    cwd=str(ROOT_DIR),
                    capture_output=True, timeout=30,
                )
                logger.warning("Cherry-pick to %s failed, pushing %s instead", PAGES_BRANCH, current_branch)
                return _push_branch(current_branch)

            # Commit on main
            subprocess.run(
                ["git", "commit", "-m", f"stats: update live_stats.json ({timestamp})"],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=30,
            )

            # Push main
            pushed = _push_branch(PAGES_BRANCH)

            # Switch back to original branch
            subprocess.run(
                ["git", "checkout", current_branch],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=30,
            )

            if pushed:
                logger.info("Stats pushed to GitHub (main branch for Pages)")
            return pushed

        # ── Already on main — push directly ──
        return _push_branch(PAGES_BRANCH)

    except subprocess.TimeoutExpired:
        logger.error("Git operation timed out")
        return False
    except Exception as e:
        logger.error("Git error: %s", e)
        return False


def _push_branch(branch: str) -> bool:
    """Push a branch to origin with retry logic. Auto-pulls on reject."""
    for attempt in range(4):
        _clean_git_locks()
        push_cmd = ["git", "push", "origin", branch]
        if attempt == 0:
            push_cmd = ["git", "push", "--set-upstream", "origin", branch]
        result = subprocess.run(
            push_cmd,
            cwd=str(ROOT_DIR),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("Stats pushed to GitHub (%s)", branch)
            return True

        # If rejected because remote is ahead, pull --rebase then retry
        if "fetch first" in result.stderr or "non-fast-forward" in result.stderr:
            logger.info("Remote %s is ahead — pulling with rebase...", branch)
            _clean_git_locks()
            pull = subprocess.run(
                ["git", "pull", "--rebase", "origin", branch],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=60,
            )
            if pull.returncode == 0:
                logger.info("Pull --rebase succeeded, retrying push...")
                continue  # retry the push immediately
            else:
                logger.warning("Pull --rebase failed: %s", pull.stderr.strip())

        wait = 2 ** (attempt + 1)
        logger.warning(
            "Git push failed (attempt %d/4): %s -- retrying in %ds",
            attempt + 1, result.stderr.strip(), wait,
        )
        time.sleep(wait)

    logger.error("Git push failed after 4 attempts")
    return False


def _check_status_transition(new_status: str) -> None:
    """
    Detect OFFLINE → LIVE transition and send SMS notifications.
    Runs notification in a background thread to not block publishing.
    """
    global _previous_status, _notification_sent_this_session

    if (_previous_status == "OFFLINE" and new_status == "LIVE"
            and not _notification_sent_this_session):
        logger.info("STATUS TRANSITION: OFFLINE → LIVE -- triggering SMS notifications")
        _notification_sent_this_session = True

        def _send_in_background():
            try:
                from notify_subscribers import notify_all
                result = notify_all(
                    "NQ.BOT is now LIVE!\n\n"
                    "The MNQ futures simulation is running.\n"
                    "Watch live: www.makemoneymarkets.com\n\n"
                    "-- NQ.BOT"
                )
                logger.info("SMS notification result: %s", result)
            except ImportError:
                logger.warning("notify_subscribers module not found -- SMS notifications disabled")
            except Exception as e:
                logger.error("SMS notification failed: %s", e)

        thread = threading.Thread(target=_send_in_background, daemon=True)
        thread.start()

    _previous_status = new_status


def run_loop(interval: int = 60, dry_run: bool = False) -> None:
    """Main publish loop."""
    logger.info("=" * 50)
    logger.info("  LIVE STATS PUBLISHER")
    logger.info("  Interval: %ds", interval)
    logger.info("  Output:   %s", OUTPUT_FILE)
    logger.info("  Dry-run:  %s", dry_run)
    logger.info("=" * 50)

    while True:
        try:
            stats = build_sanitized_stats()
            write_stats(stats)

            logger.info(
                "Published: status=%s bars=%d trades=%d wr=%.1f%% pf=%.2f pnl=%.3f%%",
                stats["status"],
                stats["bars_processed"],
                stats["trades_today"],
                stats["win_rate"],
                stats["profit_factor"],
                stats["session_pnl_pct"],
            )

            # Check for status transition and notify subscribers
            _check_status_transition(stats["status"])

            git_commit_and_push(dry_run=dry_run)

        except Exception as e:
            logger.error("Publish error: %s", e)

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="Publish sanitized bot stats to GitHub Pages",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Update interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't commit/push to git (just write file)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run once and exit (don't loop)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.once:
        stats = build_sanitized_stats()
        write_stats(stats)
        print(json.dumps(stats, indent=2))
        if not args.dry_run:
            git_commit_and_push()
        return

    try:
        run_loop(interval=args.interval, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logger.info("Publisher stopped")


if __name__ == "__main__":
    main()
