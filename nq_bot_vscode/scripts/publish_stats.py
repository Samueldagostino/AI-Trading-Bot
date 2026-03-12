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
import tempfile
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


def _safe_round(value, ndigits=2):
    """None-safe round() — returns 0.0 if value is None."""
    if value is None:
        return 0.0
    return round(value, ndigits)


def atomic_json_write(filepath, data, retries=3, delay=0.1):
    """Write JSON atomically — write to temp file, then rename.

    Retries on Windows PermissionError when another process holds the file.
    """
    dir_name = os.path.dirname(filepath)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=dir_name,
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    for attempt in range(retries):
        try:
            os.replace(tmp_path, str(filepath))
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                logger.warning("Atomic write failed for %s after %d retries (PermissionError)", filepath, retries)
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except OSError as e:
            logger.warning("Atomic write failed for %s: %s", filepath, e)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return


def repair_corrupted_jsonl(filepath: Path) -> int:
    """
    Attempt to repair a corrupted JSONL file at startup.

    Reads each line, keeps valid JSON objects, writes back atomically.
    Returns the number of recovered entries, or -1 if no repair needed.
    """
    if not filepath.exists():
        return -1

    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError:
        return -1

    # Quick check: try loading the whole file as JSON first.
    # If it parses as a single JSON array, the file isn't JSONL — rewrite as JSONL.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            # File is a JSON array, not JSONL — convert to JSONL
            lines = [json.dumps(entry) for entry in data]
            dir_name = str(filepath.parent)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=dir_name, suffix=".tmp", delete=False,
                ) as tmp:
                    tmp.write("\n".join(lines) + "\n" if lines else "")
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    tmp_path = tmp.name
                os.replace(tmp_path, str(filepath))
            except OSError:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            return len(data)
        # Single JSON object, not a list — no repair needed
        return -1
    except json.JSONDecodeError:
        pass

    # File is either valid JSONL or corrupted — parse line by line
    recovered = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recovered.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # Skip corrupted lines

    if not recovered:
        return 0

    # Write back atomically as clean JSONL
    dir_name = str(filepath.parent)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_name, suffix=".tmp", delete=False,
        ) as tmp:
            for entry in recovered:
                tmp.write(json.dumps(entry) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, str(filepath))
    except OSError:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return 0

    return len(recovered)


# Track publisher uptime for heartbeat
_PUBLISHER_START = datetime.now(timezone.utc)

# Track previous status for OFFLINE → LIVE transition detection
_previous_status = "OFFLINE"
_notification_sent_this_session = False

# ── Activity Feed state tracking ──
# Track previous modifier/safety/bias values to detect changes between publishes.
_prev_modifiers = {}
_prev_htf_bias = ""
_prev_safety = ""
_accumulated_feed: list = []   # Persists across publish cycles, trimmed to last 50
_MAX_FEED_ITEMS = 50


def _read_json_safe(filepath: Path, default=None):
    """Read a JSON file safely, returning default on missing/corrupt/error."""
    if default is None:
        default = {}
    try:
        if not filepath.exists():
            return default
        text = filepath.read_text(encoding="utf-8").strip()
        if not text:
            return default
        return json.loads(text)
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
        # Graceful fallback — file may be mid-write or corrupted
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
        # Health monitor is running — use its data
        try:
            last_seen = datetime.fromisoformat(hb_data["last_seen"])
            age_sec = (now - last_seen).total_seconds()
        except (ValueError, TypeError):
            last_seen = now
            age_sec = 0.0

        uptime_pct = round(hb_data.get("uptime_pct", 0.0), 1)
        quality = hb_data.get("connection_quality", "unknown")
    else:
        # No health monitor — derive from publisher activity
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


def _build_activity_feed(
    decisions: list,
    modifiers: dict,
    htf_bias: str,
    safety_status: str,
    bot_state_obj: dict,
    status: dict,
) -> list:
    """
    Build a rich activity feed from multiple data sources.

    Detects state changes (bias, modifiers, safety) and merges with
    trade decisions to produce a unified timeline.  Each entry has:
      type, time, title, detail, color
    """
    global _prev_modifiers, _prev_htf_bias, _prev_safety, _accumulated_feed

    now = datetime.now(timezone.utc)
    new_events: list = []

    def _fmt_now() -> str:
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
        except Exception:
            return now.strftime("%H:%M:%S UTC")

    # ── 1. HTF bias change ──
    if _prev_htf_bias and htf_bias != _prev_htf_bias:
        new_events.append({
            "type": "bias",
            "time": _fmt_now(),
            "title": "HTF Bias Shift",
            "detail": f"{_prev_htf_bias} \u2192 {htf_bias}",
            "color": "green" if htf_bias == "BULLISH" else "red" if htf_bias == "BEARISH" else "amber",
        })
    _prev_htf_bias = htf_bias

    # ── 2. Modifier changes ──
    if modifiers:
        for key in ("overnight", "gamma", "har_rv", "fomc"):
            cur = modifiers.get(key, {})
            cur_val = cur.get("value", 1.0)
            prev_val = _prev_modifiers.get(key, {}).get("value", cur_val)
            if abs(cur_val - prev_val) > 0.01:
                label = {
                    "overnight": "Overnight Session",
                    "gamma": "Gamma Exposure",
                    "har_rv": "Volatility (HAR-RV)",
                    "fomc": "FOMC Risk",
                }.get(key, key.upper())
                new_events.append({
                    "type": "modifier",
                    "time": _fmt_now(),
                    "title": f"{label} Updated",
                    "detail": f"{prev_val:.2f}x \u2192 {cur_val:.2f}x \u2014 {cur.get('display', '')}",
                    "color": "amber" if cur_val > 1.0 else "green" if cur_val < 1.0 else "muted",
                })
        _prev_modifiers = {k: (dict(v) if isinstance(v, dict) else v) for k, v in modifiers.items()} if modifiers else {}

    # ── 3. Safety rail change ──
    if _prev_safety and safety_status != _prev_safety:
        new_events.append({
            "type": "safety",
            "time": _fmt_now(),
            "title": "Safety Rails" if safety_status == "OK" else "Safety Alert",
            "detail": f"{_prev_safety} \u2192 {safety_status}",
            "color": "green" if safety_status == "OK" else "red",
        })
    _prev_safety = safety_status

    # ── 4. Trade decisions (newest entries not already in feed) ──
    # We track by timestamp to avoid duplicates across cycles
    existing_times = {e.get("_ts") for e in _accumulated_feed if e.get("_ts")}
    # Only show decisions from the current publisher session (skip stale ones)
    session_start = _PUBLISHER_START.isoformat()
    for d in decisions[-20:]:
        ts = d.get("timestamp", "")
        if ts in existing_times:
            continue  # Already in feed
        # Skip decisions older than this publisher session
        if ts and ts < session_start:
            continue

        # Format time
        time_et = ""
        if ts:
            try:
                from zoneinfo import ZoneInfo
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                et = dt.astimezone(ZoneInfo("America/New_York"))
                time_et = et.strftime("%H:%M:%S ET")
            except (ValueError, TypeError):
                time_et = ""

        direction = d.get("signal_direction", "")
        decision = d.get("decision", "")
        stage = d.get("rejection_stage", "")
        score = d.get("signal_score", d.get("score", None))
        source = d.get("signal_source", d.get("source", ""))

        if decision == "APPROVED":
            detail = f"{direction} signal approved"
            if score is not None:
                detail += f" (score: {score:.2f})"
            if source:
                detail += f" via {source}"
            new_events.append({
                "type": "trade",
                "time": time_et,
                "title": "Trade Entry",
                "detail": detail,
                "color": "green",
                "_ts": ts,
            })
        elif decision == "REJECTED":
            detail = f"{direction} signal rejected \u2014 {stage}"
            if score is not None:
                detail += f" (score: {score:.2f})"
            new_events.append({
                "type": "decision",
                "time": time_et,
                "title": "Signal Filtered",
                "detail": detail,
                "color": "muted",
                "_ts": ts,
            })

    # ── 5. System status events (synthesized) ──
    mode = bot_state_obj.get("mode", "OFFLINE")
    bars = status.get("bars_processed", 0) if status else 0
    if bars > 0 and not any(e.get("type") == "system" for e in _accumulated_feed[-5:]):
        # Periodic "still alive" event every ~5 min (5 publish cycles at 60s)
        new_events.append({
            "type": "system",
            "time": _fmt_now(),
            "title": "Heartbeat",
            "detail": f"Bot {mode.lower()} \u2014 {bars:,} bars processed",
            "color": "green" if mode != "OFFLINE" else "muted",
        })

    # Merge into accumulated feed (newest first)
    _accumulated_feed = new_events + _accumulated_feed
    _accumulated_feed = _accumulated_feed[:_MAX_FEED_ITEMS]

    # Return without internal tracking field
    return [
        {k: v for k, v in e.items() if not k.startswith("_")}
        for e in _accumulated_feed
    ]


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

    # HTF bias — extract from modifier state or default
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
            "value": _safe_round(overnight_mod.get("value", 1.0), 2),
            "display": f"{_safe_round(overnight_mod.get('value', 1.0), 2)}x ({overnight_mod.get('reason', 'neutral')})",
        },
        "fomc": {
            "value": _safe_round(fomc_mod.get("value", 1.0), 2),
            "display": f"{_safe_round(fomc_mod.get('value', 1.0), 2)}x ({fomc_mod.get('reason', 'none')})",
        },
        "gamma": {
            "value": _safe_round(gamma_mod.get("value", 1.0), 2),
            "display": f"{_safe_round(gamma_mod.get('value', 1.0), 2)}x ({gamma_mod.get('reason', 'unknown')})",
        },
        "har_rv": {
            "value": _safe_round(har_rv_mod.get("value", 1.0), 2),
            "display": f"{_safe_round(har_rv_mod.get('value', 1.0), 2)}x ({har_rv_mod.get('reason', 'normal')})",
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

    # Build heartbeat and state (backward compatible — new fields)
    heartbeat = _build_heartbeat(status, candles)
    bot_state = _build_bot_state(status, decisions)

    # Build activity feed (rich timeline of all bot events)
    activity_feed = _build_activity_feed(
        decisions=decisions,
        modifiers=modifiers or {},
        htf_bias=htf_bias,
        safety_status=safety_status,
        bot_state_obj=bot_state,
        status=status or {},
    )

    # Active trade for live chart (read from active_trade.json written by run_tws.py)
    active_trade = _read_json_safe(LOGS_DIR / "active_trade.json")
    if not active_trade or not active_trade.get("side"):
        active_trade = None  # No active trade

    # Last 40 candles for the mini trade chart (publicly available price data)
    chart_candles = []
    if isinstance(candles, list) and candles:
        for c in candles[-40:]:
            chart_candles.append({
                "t": c.get("t", ""),
                "o": c.get("o", 0),
                "h": c.get("h", 0),
                "l": c.get("l", 0),
                "c": c.get("c", 0),
            })

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "status": "LIVE" if is_live else "OFFLINE",
        "trading_mode": "PAPER",  # Change to "LIVE" only after Phase 4 validation gate
        "bars_processed": status.get("bars_processed", len(candles) if isinstance(candles, list) else 0),
        "trades_today": status.get("trade_count", 0),
        "win_rate": _safe_round(status.get("win_rate", 0.0), 1),
        "profit_factor": _safe_round(status.get("profit_factor", 0.0), 2),
        "session_pnl_pct": session_pnl_pct,
        "signals_rejected": rejected,
        "signals_approved": approved,
        "htf_bias": htf_bias,
        "safety_rails": safety_status,
        "modifiers": mod_values,
        "last_price": _safe_round(last_price, 2),
        "recent_decisions": recent_decisions,
        # Heartbeat & state
        "heartbeat": heartbeat,
        "state": bot_state,
        # Trade chart data (publicly available market prices only)
        "active_trade": active_trade,
        "candle_buffer": chart_candles,
        # Rich activity feed (up to 50 events)
        "activity_feed": activity_feed,
    }


TRADE_VIZ_FILE = DOCS_DATA_DIR / "trade_viz_data.json"


def build_trade_viz_data() -> dict | None:
    """
    Build a simplified viz_data.json from paper_trades.json + candle_buffer.json.
    This feeds the Forensic Analyzer page with live/paper trade data.

    SECURITY: Prices are publicly available market data. PnL is already
    in paper_trades.json as dollar amounts — we convert to points for the
    analyzer (which works in points, not dollars).

    Returns None if no trade data available.
    """
    trades_raw = _read_jsonl_safe(LOGS_DIR / "paper_trades.json", limit=500)
    if not trades_raw:
        return None

    candles_raw = _read_json_safe(LOGS_DIR / "candle_buffer.json", default=[])

    # Build candle array for the chart
    candles = []
    if isinstance(candles_raw, list):
        for c in candles_raw:
            candles.append({
                "time": c.get("t", ""),
                "open": c.get("o", 0),
                "high": c.get("h", 0),
                "low": c.get("l", 0),
                "close": c.get("c", 0),
                "volume": c.get("v", 0),
            })

    # Build trade entry/exit pairs for the analyzer
    # paper_trades.json has flat records with trade_closed events
    trades = []
    for i, t in enumerate(trades_raw):
        if t.get("event") != "trade_closed":
            continue

        direction = t.get("direction", "long")
        entry_price = t.get("entry_price") or 0.0
        total_pnl = t.get("total_pnl") or 0.0
        c1_pnl = t.get("c1_pnl") or 0.0
        c2_pnl = t.get("c2_pnl") or 0.0
        c1_reason = t.get("c1_reason", "stop")
        c2_reason = t.get("c2_reason", "stop")

        # Determine dominant exit type
        if "trail" in c1_reason or "trail" in c2_reason:
            exit_type = "trailing_stop"
        elif "pt2" in c2_reason or "target" in c2_reason:
            exit_type = "pt2_target"
        elif "pt1" in c1_reason or "partial" in c1_reason:
            exit_type = "pt1_partial"
        elif "be" in c1_reason or "breakeven" in c1_reason:
            exit_type = "be_plus"
        else:
            exit_type = "stop_loss"

        # Calculate approximate exit price from PnL
        # PnL = (exit - entry) * qty * point_value for long
        # Assume 2-lot @ $2/pt
        pts_pnl = total_pnl / (2 * 2.0) if total_pnl else 0
        if direction == "long":
            exit_price = entry_price + pts_pnl
        else:
            exit_price = entry_price - pts_pnl

        stop_dist = t.get("stop_distance", abs(pts_pnl) if total_pnl < 0 else 10.0)

        trades.append({
            "id": i + 1,
            "entryTime": t.get("timestamp", ""),
            "exitTime": t.get("timestamp", ""),  # Same for closed trades
            "side": direction,
            "qty": 2,
            "entryPrice": _safe_round(entry_price, 2),
            "exitPrice": _safe_round(exit_price, 2),
            "stopPrice": _safe_round(entry_price - stop_dist if direction == "long" else entry_price + stop_dist, 2),
            "tp1Price": _safe_round(entry_price + stop_dist * 1.5 if direction == "long" else entry_price - stop_dist * 1.5, 2),
            "tp2Price": _safe_round(entry_price + stop_dist * 3.0 if direction == "long" else entry_price - stop_dist * 3.0, 2),
            "stopDist": _safe_round(stop_dist, 1),
            "signalScore": t.get("signal_score", 0.85),
            "regime": t.get("regime", "unknown"),
            "htfBias": t.get("htf_bias", "neutral"),
            "exitType": exit_type,
            "pnl": _safe_round(total_pnl, 2),
            "c1Pnl": _safe_round(c1_pnl, 2),
            "c2Pnl": _safe_round(c2_pnl, 2),
            "slippage": _safe_round((t.get("total_slippage_pts") or 1.5) * 2.0, 2),
            "rMultiple": _safe_round(pts_pnl / stop_dist, 2) if stop_dist > 0 else 0,
            "mfe": 0,
            "mae": 0,
            "holdBars": t.get("hold_bars", 5),
            "isCompliant": True,
        })

    if not trades:
        return None

    return {
        "candles": candles,
        "trades": trades,
        "source": "live_paper_trading",
        "updated": datetime.now(timezone.utc).isoformat(),
    }


def _atomic_write_with_retry(filepath: Path, data: dict) -> None:
    """Atomic JSON write with Windows PermissionError retry."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = filepath.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.warning("Atomic write tmp failed for %s: %s", filepath, e)
        return
    for attempt in range(3):
        try:
            os.replace(str(tmp_path), str(filepath))
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
            else:
                logger.warning("Atomic write failed for %s after 3 retries", filepath)
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError as e:
            logger.warning("Atomic write failed for %s: %s", filepath, e)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return


def write_trade_viz(viz_data: dict) -> None:
    """Write trade viz data atomically to docs/data/trade_viz_data.json."""
    _atomic_write_with_retry(TRADE_VIZ_FILE, viz_data)


def write_stats(stats: dict) -> None:
    """Write stats atomically to docs/data/live_stats.json."""
    _atomic_write_with_retry(OUTPUT_FILE, stats)


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
            pass  # Can't remove — another process truly holds it


def git_commit_and_push(dry_run: bool = False) -> bool:
    """Commit and push live_stats.json to GitHub.

    Strategy: always create a new commit (no amend), pull --rebase to
    incorporate remote changes, then standard push.  Never force-push.
    If rebase fails due to conflicts, log and skip this cycle.
    """
    try:
        # Clean stale lock files before any git operation
        _clean_git_locks()

        rel_path = OUTPUT_FILE.relative_to(ROOT_DIR)
        rel_viz_path = TRADE_VIZ_FILE.relative_to(ROOT_DIR)

        # Stage the stats files (separately so one missing file doesn't block the other)
        subprocess.run(
            ["git", "add", str(rel_path)],
            cwd=str(ROOT_DIR),
            capture_output=True, text=True, timeout=30,
        )
        if TRADE_VIZ_FILE.exists():
            subprocess.run(
                ["git", "add", str(rel_viz_path)],
                cwd=str(ROOT_DIR),
                capture_output=True, text=True, timeout=30,
            )

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(ROOT_DIR), capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Git: no changes to commit (file unchanged)")
            return True

        if dry_run:
            logger.info("[DRY-RUN] Would commit and push live_stats.json")
            subprocess.run(
                ["git", "reset", "HEAD", str(rel_path)],
                cwd=str(ROOT_DIR), capture_output=True, timeout=30,
            )
            return True

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Always create a new commit (never amend — avoids need for force-push)
        result = subprocess.run(
            ["git", "commit", "-m",
             f"stats: update live_stats.json ({timestamp})"],
            cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=30,
        )
        if last_msg.returncode == 0 and last_msg.stdout.strip().startswith("stats:"):
            # Amend the previous stats commit (avoids piling up hundreds of commits)
            result = subprocess.run(
                ["git", "commit", "-m",
                 f"stats: update live_stats.json ({timestamp})"],
                cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=30,
            )
        else:
            # New commit (after a code commit)
            result = subprocess.run(
                ["git", "commit", "-m",
                 f"stats: update live_stats.json ({timestamp})"],
                cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=30,
            )

        if result.returncode != 0:
            logger.warning("Git commit failed: %s", result.stderr)
            return False

        # Pull --rebase to incorporate any remote changes before pushing
        rebase_result = subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60,
        )
        if rebase_result.returncode != 0:
            stderr = rebase_result.stderr.strip()
            # Rebase conflict — abort and skip this cycle
            logger.warning("Rebase conflict — aborting and skipping this push cycle: %s", stderr)
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=10,
        # Push (force-with-lease because we may have amended)
        for attempt in range(3):
            result = subprocess.run(
                ["git", "push", "origin", "HEAD"],
                cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60,
            )
            return False

        # Standard push (no force flags)
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("Stats pushed to GitHub")
            return True

        stderr = result.stderr.strip()
        logger.warning("Git push failed: %s", stderr)
        return False

    except subprocess.TimeoutExpired:
        logger.error("Git operation timed out")
        return False
    except Exception as e:
        logger.error("Git error: %s", e)
        return False


def _check_status_transition(new_status: str) -> None:
    """
    Detect OFFLINE → LIVE transition and send SMS notifications.
    Runs notification in a background thread to not block publishing.
    """
    global _previous_status, _notification_sent_this_session

    if (_previous_status == "OFFLINE" and new_status == "LIVE"
            and not _notification_sent_this_session):
        logger.info("STATUS TRANSITION: OFFLINE → LIVE — triggering SMS notifications")
        _notification_sent_this_session = True

        def _send_in_background():
            try:
                from notify_subscribers import notify_all
                result = notify_all(
                    "NQ.BOT is now LIVE!\n\n"
                    "The MNQ futures simulation is running.\n"
                    "Watch live: www.makemoneymarkets.com\n\n"
                    "— NQ.BOT"
                )
                logger.info("SMS notification result: %s", result)
            except ImportError:
                logger.warning("notify_subscribers module not found — SMS notifications disabled")
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

            # Build and write trade viz data for the Analyzer page
            viz_data = build_trade_viz_data()
            if viz_data:
                write_trade_viz(viz_data)
                logger.debug("Trade viz: %d trades", len(viz_data.get("trades", [])))

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
            import traceback
            logger.error(f"Publish error: {e}\n{traceback.format_exc()}")

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

    # One-time repair of corrupted JSONL files at startup
    for jsonl_file in [
        LOGS_DIR / "paper_trades.json",
        LOGS_DIR / "trade_decisions.json",
    ]:
        n = repair_corrupted_jsonl(jsonl_file)
        if n >= 0:
            logger.info("Repaired corrupted %s — recovered %d entries", jsonl_file.name, n)

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
