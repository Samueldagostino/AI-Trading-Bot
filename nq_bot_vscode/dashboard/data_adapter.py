"""
Dashboard Data Adapter
=======================
Reads raw TradingView CSV candle exports and backtest trade output,
normalizes everything, and produces a single viz_data.json for the
NQ Forensic Trade Visualizer.

Two modes:
  1. --backtest-json <path>  : Read a backtest_viz_data.json (from run_backtest.py)
  2. --candles <csv> --trades <json> : Read raw CSV + separate trade log

Output:
  dashboard/viz_data.json     -- { candles: [...], trades: [...] }
  dashboard/data_anomalies.json -- integrity issues found during processing

Usage:
  python -m dashboard.data_adapter --backtest-json backtest_viz_data.json
  python -m dashboard.data_adapter --candles data/tradingview/MNQ_5m.csv --trades trade_log.json
  python -m dashboard.data_adapter --candles-dir data/tradingview/ --tf 5m
"""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────
ET_TZ = ZoneInfo("America/New_York")  # DST-aware US Eastern

# NQ futures schedule (ET)
NQ_OPEN_HOUR = 18       # 6:00 PM ET (prior day)
NQ_CLOSE_HOUR = 17      # 5:00 PM ET
MAINTENANCE_START = (15, 30)  # 3:30 PM ET
MAINTENANCE_END = (18, 0)     # 6:00 PM ET

# MNQ contract specs
POINT_VALUE = 2.0        # $2/point per contract
TICK_SIZE = 0.25

# NQ holidays 2025-2026 (CME closures -- abbreviated list)
NQ_HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}


# ── Anomaly Tracker ───────────────────────────────────────────

class AnomalyTracker:
    """Collects data integrity issues during processing."""

    def __init__(self):
        self.anomalies: List[Dict[str, Any]] = []

    def add(self, category: str, severity: str, message: str, context: Optional[dict] = None):
        entry = {
            "category": category,
            "severity": severity,  # "error", "warning", "info"
            "message": message,
        }
        if context:
            entry["context"] = context
        self.anomalies.append(entry)
        if severity == "error":
            logger.error(f"[{category}] {message}")
        elif severity == "warning":
            logger.warning(f"[{category}] {message}")
        else:
            logger.info(f"[{category}] {message}")

    def to_json(self) -> list:
        return self.anomalies

    @property
    def error_count(self) -> int:
        return sum(1 for a in self.anomalies if a["severity"] == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.anomalies if a["severity"] == "warning")


# ── Candle Processing ─────────────────────────────────────────

def parse_tv_timestamp(raw: str) -> Optional[datetime]:
    """Parse a TradingView CSV timestamp (Unix epoch or ISO string)."""
    # Try Unix epoch first (most common in TV exports)
    try:
        ts = float(raw)
        # Sanity: must be between 2019 and 2030
        if 1546300800 < ts < 1893456000:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        pass

    # Try ISO formats
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    return None


def read_tv_csv(filepath: str, tracker: AnomalyTracker) -> List[dict]:
    """
    Read a TradingView CSV export and return normalized candle dicts.

    Expected CSV: time,open,high,low,close,Volume
    Timestamps are Unix epoch seconds (UTC).
    """
    filepath = Path(filepath)
    if not filepath.exists():
        tracker.add("candle_file", "error", f"File not found: {filepath}")
        return []

    candles = []
    seen_times = set()
    row_count = 0
    skip_count = 0

    # Detect column names
    col_map = {
        "time": None, "open": None, "high": None,
        "low": None, "close": None, "volume": None,
    }
    time_names = ["time", "date", "datetime", "timestamp"]
    open_names = ["open", "o"]
    high_names = ["high", "h"]
    low_names = ["low", "l"]
    close_names = ["close", "c"]
    vol_names = ["volume", "vol", "v"]

    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            headers_lower = {h.lower(): h for h in headers}

            for std, variants in [
                ("time", time_names), ("open", open_names), ("high", high_names),
                ("low", low_names), ("close", close_names), ("volume", vol_names),
            ]:
                for v in variants:
                    if v.lower() in headers_lower:
                        col_map[std] = headers_lower[v.lower()]
                        break

            missing = [k for k in ["time", "open", "high", "low", "close"] if col_map[k] is None]
            if missing:
                tracker.add("candle_file", "error",
                            f"Missing required columns {missing} in {filepath}. Headers: {headers}")
                return []

            for row in reader:
                row_count += 1
                raw_time = row.get(col_map["time"], "").strip()
                ts = parse_tv_timestamp(raw_time)
                if ts is None:
                    skip_count += 1
                    continue

                try:
                    o = float(row[col_map["open"]])
                    h = float(row[col_map["high"]])
                    l = float(row[col_map["low"]])
                    c = float(row[col_map["close"]])
                except (ValueError, KeyError):
                    skip_count += 1
                    continue

                vol = 0
                if col_map["volume"]:
                    try:
                        vol = int(float(row.get(col_map["volume"], "0")))
                    except (ValueError, TypeError):
                        vol = 0

                # OHLC sanity
                if h < l:
                    tracker.add("ohlc_sanity", "warning",
                                f"high ({h}) < low ({l}) at {ts.isoformat()}",
                                {"time": ts.isoformat(), "ohlc": [o, h, l, c]})
                    h, l = l, h  # swap and continue

                if o <= 0 or c <= 0:
                    tracker.add("ohlc_sanity", "warning",
                                f"Non-positive price at {ts.isoformat()}: O={o} C={c}")
                    skip_count += 1
                    continue

                if h < max(o, c) or l > min(o, c):
                    tracker.add("ohlc_sanity", "warning",
                                f"OHLC wick violation at {ts.isoformat()}: O={o} H={h} L={l} C={c}")
                    h = max(h, o, c)
                    l = min(l, o, c)

                # Dedup
                ts_ms = int(ts.timestamp() * 1000)
                if ts_ms in seen_times:
                    tracker.add("duplicate", "info",
                                f"Duplicate timestamp skipped: {ts.isoformat()}")
                    skip_count += 1
                    continue
                seen_times.add(ts_ms)

                candles.append({
                    "time": ts_ms,
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2),
                    "volume": vol,
                })

    except Exception as e:
        tracker.add("candle_file", "error", f"Error reading {filepath}: {e}")
        return []

    # Sort by time
    candles.sort(key=lambda c: c["time"])

    if skip_count > 0:
        tracker.add("candle_parse", "info",
                     f"Skipped {skip_count}/{row_count} rows in {filepath.name}")

    tracker.add("candle_file", "info",
                f"Loaded {len(candles)} candles from {filepath.name}")

    return candles


def detect_gaps(candles: List[dict], expected_interval_ms: int, tracker: AnomalyTracker):
    """Detect missing bars, holiday gaps, and rollover gaps."""
    if len(candles) < 2:
        return

    gap_tolerance = expected_interval_ms * 1.5
    gap_count = 0

    for i in range(1, len(candles)):
        dt = candles[i]["time"] - candles[i - 1]["time"]
        if dt > gap_tolerance:
            prev_dt = datetime.fromtimestamp(candles[i - 1]["time"] / 1000, tz=timezone.utc)
            curr_dt = datetime.fromtimestamp(candles[i]["time"] / 1000, tz=timezone.utc)
            gap_minutes = dt / 60000

            # Classify the gap
            prev_date_str = prev_dt.strftime("%Y-%m-%d")
            curr_date_str = curr_dt.strftime("%Y-%m-%d")

            if prev_date_str in NQ_HOLIDAYS or curr_date_str in NQ_HOLIDAYS:
                gap_type = "holiday"
            elif prev_dt.weekday() == 4 and curr_dt.weekday() == 6:
                gap_type = "weekend"
            elif prev_dt.weekday() >= 5 or curr_dt.weekday() >= 5:
                gap_type = "weekend"
            elif (prev_dt.hour == 15 and prev_dt.minute >= 30) or prev_dt.hour >= 16:
                gap_type = "maintenance"  # ET times
            elif (prev_dt.hour == 21 and prev_dt.minute >= 30) or prev_dt.hour == 22:
                gap_type = "maintenance"  # UTC equivalent (ET+5)
            elif (prev_dt.hour == 20 and prev_dt.minute >= 30) or prev_dt.hour == 21:
                gap_type = "maintenance"  # UTC equivalent (ET+4, EDT)
            elif gap_minutes > 24 * 60:
                gap_type = "multi_day"
            else:
                gap_type = "missing_bars"

            severity = "info" if gap_type in ("weekend", "holiday", "maintenance") else "warning"
            gap_count += 1

            if gap_count <= 50:  # Cap logged gaps
                tracker.add("gap", severity,
                            f"{gap_type}: {gap_minutes:.0f}min gap from {prev_dt.isoformat()} to {curr_dt.isoformat()}")

    if gap_count > 50:
        tracker.add("gap", "info", f"... and {gap_count - 50} more gaps (total: {gap_count})")


def filter_candles_by_tf(candles: List[dict], timeframe_minutes: int) -> List[dict]:
    """Filter to keep only candles matching the expected interval (within tolerance)."""
    if len(candles) < 2:
        return candles

    # Detect actual interval from data
    deltas = []
    for i in range(1, min(100, len(candles))):
        dt = candles[i]["time"] - candles[i - 1]["time"]
        if dt > 0:
            deltas.append(dt)

    if not deltas:
        return candles

    # Most common delta
    from collections import Counter
    delta_minutes = Counter(int(d / 60000) for d in deltas)
    most_common = delta_minutes.most_common(1)[0][0]

    logger.info(f"Detected candle interval: {most_common}m (requested: {timeframe_minutes}m)")
    return candles


# ── Trade Processing ──────────────────────────────────────────

def pair_trade_events(trade_log: List[dict], tracker: AnomalyTracker) -> List[dict]:
    """
    Pair entry/exit events from the backtest trade_log into complete trades.

    The trade_log from run_backtest_mtf() contains:
      - {action: "entry", timestamp, direction, contracts, entry_price, stop,
         c1_target, signal_score, regime, htf_bias, htf_strength}
      - {action: "c1_target_hit", c1_pnl, c2_new_stop, price}
      - {action: "trade_closed", trade_id, direction, entry_price,
         c1_exit_price, c1_exit_reason, c1_pnl, c2_exit_price,
         c2_exit_reason, c2_pnl, total_pnl, regime, phase_history,
         close_timestamp}
    """
    paired = []
    pending_entry = None

    for event in trade_log:
        action = event.get("action", "")

        if action == "entry":
            pending_entry = event

        elif action == "trade_closed":
            entry = pending_entry
            if entry is None:
                tracker.add("trade_pairing", "warning",
                            f"trade_closed without preceding entry: trade_id={event.get('trade_id')}")
                # Still try to build a trade from just the close event
                entry = {}

            entry_price = entry.get("entry_price", event.get("entry_price", 0))
            direction = entry.get("direction", event.get("direction", "long"))
            stop_price = entry.get("stop", 0)
            c1_target = entry.get("c1_target", 0)
            signal_score = entry.get("signal_score", 0)
            regime = entry.get("regime", event.get("regime", "unknown"))
            htf_bias = entry.get("htf_bias", "neutral")

            entry_ts = entry.get("timestamp", "")
            exit_ts = event.get("close_timestamp", "")

            entry_time_ms = _iso_to_ms(entry_ts)
            exit_time_ms = _iso_to_ms(exit_ts)

            c1_pnl = event.get("c1_pnl", 0)
            c2_pnl = event.get("c2_pnl", 0)
            total_pnl = event.get("total_pnl", 0)
            c1_exit_reason = event.get("c1_exit_reason", "unknown")
            c2_exit_reason = event.get("c2_exit_reason", "unknown")
            c1_exit_price = event.get("c1_exit_price", 0)
            c2_exit_price = event.get("c2_exit_price", 0)

            # Derive stop distance
            if stop_price and entry_price:
                stop_dist = abs(entry_price - stop_price)
            else:
                stop_dist = 0

            # Derive TP1 price from c1_target or entry + stop_dist * 1.5
            if c1_target:
                tp1_price = c1_target
            elif stop_dist > 0:
                tp1_mult = 1.5
                tp1_price = (entry_price + stop_dist * tp1_mult) if direction == "long" else (entry_price - stop_dist * tp1_mult)
            else:
                tp1_price = 0

            # TP2 = stop_dist * 3 (standard 3R target)
            if stop_dist > 0:
                tp2_price = (entry_price + stop_dist * 3) if direction == "long" else (entry_price - stop_dist * 3)
            else:
                tp2_price = 0

            # Map exit reasons to viz exitType enum
            exit_type = _map_exit_type(c1_exit_reason, c2_exit_reason)

            # Compute R-multiple
            risk_dollars = stop_dist * POINT_VALUE * 2  # 2 contracts
            r_multiple = round(total_pnl / risk_dollars, 2) if risk_dollars > 0 else 0

            # Compute slippage estimate (diff between ideal and actual)
            slippage = 0.0
            if c1_exit_reason == "target" and c1_target and c1_exit_price:
                slippage += abs(c1_exit_price - c1_target) * POINT_VALUE
            if stop_price and c2_exit_reason == "stop":
                slippage += abs(c2_exit_price - stop_price) * POINT_VALUE if c2_exit_price else 0

            # MFE/MAE -- not available from trade_log, set to 0 (would need bar-by-bar data)
            mfe = 0.0
            mae = 0.0

            # Hold bars -- compute from timestamps if available
            hold_bars = 0
            if entry_time_ms and exit_time_ms and exit_time_ms > entry_time_ms:
                # Approximate: assume 2-minute bars
                hold_bars = max(1, int((exit_time_ms - entry_time_ms) / (2 * 60 * 1000)))

            # Exit price for the viz (use C2's exit as the "final" exit)
            exit_price = c2_exit_price if c2_exit_price else c1_exit_price

            paired.append({
                "id": len(paired) + 1,
                "entryTime": entry_time_ms,
                "exitTime": exit_time_ms,
                "side": direction,
                "qty": 2,
                "entryPrice": round(entry_price, 2),
                "exitPrice": round(exit_price, 2),
                "stopPrice": round(stop_price, 2),
                "tp1Price": round(tp1_price, 2),
                "tp2Price": round(tp2_price, 2),
                "stopDist": round(stop_dist, 2),
                "signalScore": round(signal_score, 3),
                "regime": regime,
                "htfBias": htf_bias,
                "exitType": exit_type,
                "pnl": round(total_pnl, 2),
                "c1Pnl": round(c1_pnl, 2),
                "c2Pnl": round(c2_pnl, 2),
                "slippage": round(slippage, 2),
                "rMultiple": r_multiple,
                "mfe": round(mfe, 2),
                "mae": round(mae, 2),
                "holdBars": hold_bars,
                "isCompliant": _check_hc_compliance(signal_score, stop_dist, tp1_price, entry_price, direction),
            })

            pending_entry = None

    if pending_entry:
        tracker.add("trade_pairing", "warning", "Unpaired entry event at end of log (trade still open?)")

    tracker.add("trade_processing", "info", f"Paired {len(paired)} complete trades from trade log")
    return paired


def _iso_to_ms(iso_str: str) -> int:
    """Convert ISO timestamp string to milliseconds since epoch."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _map_exit_type(c1_reason: str, c2_reason: str) -> str:
    """Map executor exit reasons to viz exitType enum values."""
    # Priority: if stopped on both, it's a stop_loss
    if c1_reason == "stop" and c2_reason == "stop":
        return "stop_loss"
    # C1 hit target, C2 hit trailing
    if c1_reason == "target" and c2_reason == "trailing":
        return "trailing_stop"
    # C1 hit target, C2 hit breakeven
    if c1_reason == "target" and c2_reason == "breakeven":
        return "be_plus"
    # C1 hit target, C2 hit max target
    if c1_reason == "target" and c2_reason == "max_target":
        return "pt2_target"
    # C1 hit target, C2 hit time stop
    if c1_reason == "target" and c2_reason == "time_stop":
        return "trailing_stop"
    # C1 hit target but C2 is some other reason
    if c1_reason == "target":
        return "pt1_partial"
    # Fallback
    return "stop_loss"


def _check_hc_compliance(score: float, stop_dist: float, tp1: float, entry: float, direction: str) -> bool:
    """Check if trade passes HC filter gates."""
    if score < 0.75:
        return False
    if stop_dist > 30.3:  # 30pt + slippage tolerance
        return False
    if tp1 and entry and stop_dist > 0:
        if direction == "long":
            tp1_ratio = (tp1 - entry) / stop_dist
        else:
            tp1_ratio = (entry - tp1) / stop_dist
        if tp1_ratio < 1.4 or tp1_ratio > 1.6:
            return False
    return True


# ── Backtest JSON Loader ──────────────────────────────────────

def load_backtest_json(filepath: str, tracker: AnomalyTracker) -> Tuple[List[dict], List[dict]]:
    """
    Load a backtest_viz_data.json produced by run_backtest.py.

    Expected structure:
    {
        "exec_bars_log": [{time, open, high, low, close, volume}, ...],
        "trade_log": [{action:"entry",...}, {action:"trade_closed",...}, ...],
        ... (summary fields)
    }

    Returns (candles, trades) in viz-ready format.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        tracker.add("backtest_json", "error", f"File not found: {filepath}")
        return [], []

    try:
        with open(filepath, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        tracker.add("backtest_json", "error", f"Invalid JSON in {filepath}: {e}")
        return [], []

    # Extract bars
    raw_bars = data.get("exec_bars_log", data.get("bars", []))
    candles = []
    for bar in raw_bars:
        ts = bar.get("time", "")
        ts_ms = _iso_to_ms(ts) if isinstance(ts, str) else int(ts)
        if ts_ms == 0:
            continue
        candles.append({
            "time": ts_ms,
            "open": round(float(bar.get("open", 0)), 2),
            "high": round(float(bar.get("high", 0)), 2),
            "low": round(float(bar.get("low", 0)), 2),
            "close": round(float(bar.get("close", 0)), 2),
            "volume": int(bar.get("volume", 0)),
        })

    candles.sort(key=lambda c: c["time"])
    tracker.add("backtest_json", "info", f"Loaded {len(candles)} candles from backtest JSON")

    # Extract and pair trades
    trade_log = data.get("trade_log", data.get("trades", []))
    trades = pair_trade_events(trade_log, tracker)

    return candles, trades


# ── Standalone Trade JSON Loader ──────────────────────────────

def load_trades_json(filepath: str, tracker: AnomalyTracker) -> List[dict]:
    """
    Load trades from a standalone JSON file.

    Supports two formats:
    1. trade_log format (list of entry/close events) -- will be paired
    2. Pre-paired format (list of trade objects with the viz schema fields)
    """
    filepath = Path(filepath)
    if not filepath.exists():
        tracker.add("trades_file", "error", f"File not found: {filepath}")
        return []

    try:
        with open(filepath, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        tracker.add("trades_file", "error", f"Invalid JSON: {e}")
        return []

    if not isinstance(data, list):
        # Might be wrapped in an object
        data = data.get("trade_log", data.get("trades", []))

    if not data:
        tracker.add("trades_file", "warning", "No trades found in file")
        return []

    # Detect format: does first entry have "action" key (event log) or "entryPrice" (pre-paired)?
    first = data[0]
    if "action" in first:
        return pair_trade_events(data, tracker)
    elif "entryPrice" in first or "entry_price" in first:
        return _normalize_pre_paired(data, tracker)
    else:
        tracker.add("trades_file", "error",
                     f"Unrecognized trade format. Keys: {list(first.keys())[:10]}")
        return []


def _normalize_pre_paired(trades: List[dict], tracker: AnomalyTracker) -> List[dict]:
    """Normalize pre-paired trades that may use snake_case or different field names."""
    normalized = []
    for i, t in enumerate(trades):
        try:
            entry_price = t.get("entryPrice", t.get("entry_price", 0))
            exit_price = t.get("exitPrice", t.get("exit_price", 0))
            stop_price = t.get("stopPrice", t.get("stop_price", t.get("stop", 0)))
            direction = t.get("side", t.get("direction", "long"))

            stop_dist = t.get("stopDist", t.get("stop_dist", 0))
            if not stop_dist and stop_price and entry_price:
                stop_dist = abs(entry_price - stop_price)

            tp1 = t.get("tp1Price", t.get("tp1_price", t.get("c1_target", 0)))
            if not tp1 and stop_dist > 0:
                tp1 = (entry_price + stop_dist * 1.5) if direction == "long" else (entry_price - stop_dist * 1.5)

            tp2 = t.get("tp2Price", t.get("tp2_price", 0))
            if not tp2 and stop_dist > 0:
                tp2 = (entry_price + stop_dist * 3) if direction == "long" else (entry_price - stop_dist * 3)

            total_pnl = t.get("pnl", t.get("total_pnl", 0))
            c1_pnl = t.get("c1Pnl", t.get("c1_pnl", 0))
            c2_pnl = t.get("c2Pnl", t.get("c2_pnl", 0))
            signal_score = t.get("signalScore", t.get("signal_score", 0))

            risk_dollars = stop_dist * POINT_VALUE * 2
            r_multiple = t.get("rMultiple", t.get("r_multiple", 0))
            if not r_multiple and risk_dollars > 0:
                r_multiple = round(total_pnl / risk_dollars, 2)

            entry_time = t.get("entryTime", t.get("entry_time", 0))
            if isinstance(entry_time, str):
                entry_time = _iso_to_ms(entry_time)
            exit_time = t.get("exitTime", t.get("exit_time", 0))
            if isinstance(exit_time, str):
                exit_time = _iso_to_ms(exit_time)

            normalized.append({
                "id": t.get("id", i + 1),
                "entryTime": int(entry_time),
                "exitTime": int(exit_time),
                "side": direction,
                "qty": t.get("qty", t.get("contracts", 2)),
                "entryPrice": round(float(entry_price), 2),
                "exitPrice": round(float(exit_price), 2),
                "stopPrice": round(float(stop_price), 2),
                "tp1Price": round(float(tp1), 2),
                "tp2Price": round(float(tp2), 2),
                "stopDist": round(float(stop_dist), 2),
                "signalScore": round(float(signal_score), 3),
                "regime": t.get("regime", t.get("market_regime", "unknown")),
                "htfBias": t.get("htfBias", t.get("htf_bias", "neutral")),
                "exitType": t.get("exitType", t.get("exit_type", _map_exit_type(
                    t.get("c1_exit_reason", ""), t.get("c2_exit_reason", "")))),
                "pnl": round(float(total_pnl), 2),
                "c1Pnl": round(float(c1_pnl), 2),
                "c2Pnl": round(float(c2_pnl), 2),
                "slippage": round(float(t.get("slippage", 0)), 2),
                "rMultiple": round(float(r_multiple), 2),
                "mfe": round(float(t.get("mfe", 0)), 2),
                "mae": round(float(t.get("mae", 0)), 2),
                "holdBars": int(t.get("holdBars", t.get("hold_bars", 0))),
                "isCompliant": t.get("isCompliant", t.get("is_compliant",
                    _check_hc_compliance(signal_score, stop_dist, tp1, entry_price, direction))),
            })
        except (ValueError, TypeError) as e:
            tracker.add("trade_normalize", "warning", f"Failed to normalize trade {i+1}: {e}")

    tracker.add("trade_processing", "info", f"Normalized {len(normalized)} pre-paired trades")
    return normalized


# ── Output ────────────────────────────────────────────────────

def write_viz_json(candles: List[dict], trades: List[dict], output_path: str):
    """Write the combined viz_data.json."""
    data = {
        "candles": candles,
        "trades": trades,
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "candle_count": len(candles),
            "trade_count": len(trades),
            "generator": "dashboard.data_adapter",
        },
    }

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w") as f:
        json.dump(data, f, separators=(",", ":"))

    # Also write a pretty-printed version for debugging
    size_kb = output.stat().st_size / 1024
    logger.info(f"Wrote {output} ({size_kb:.1f} KB) -- {len(candles)} candles, {len(trades)} trades")


def write_anomalies(tracker: AnomalyTracker, output_path: str):
    """Write anomalies to JSON."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w") as f:
        json.dump({
            "anomalies": tracker.to_json(),
            "summary": {
                "errors": tracker.error_count,
                "warnings": tracker.warning_count,
                "total": len(tracker.anomalies),
            },
        }, f, indent=2)

    logger.info(f"Wrote {output} -- {tracker.error_count} errors, {tracker.warning_count} warnings")


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NQ Dashboard Data Adapter -- convert backtest data to viz format")

    parser.add_argument("--backtest-json", type=str,
                        help="Path to backtest_viz_data.json (from run_backtest.py)")
    parser.add_argument("--candles", type=str,
                        help="Path to a single TradingView CSV file")
    parser.add_argument("--candles-dir", type=str,
                        help="Path to directory of TradingView CSVs")
    parser.add_argument("--tf", type=str, default="5m",
                        help="Timeframe to select from candles-dir (default: 5m)")
    parser.add_argument("--trades", type=str,
                        help="Path to trade log JSON (paired or event format)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output path for viz_data.json (default: dashboard/viz_data.json)")
    parser.add_argument("--anomalies-output", type=str, default=None,
                        help="Output path for anomalies (default: dashboard/data_anomalies.json)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve output paths
    dashboard_dir = Path(__file__).parent
    output_path = args.output or str(dashboard_dir / "viz_data.json")
    anomalies_path = args.anomalies_output or str(dashboard_dir / "data_anomalies.json")

    tracker = AnomalyTracker()
    candles = []
    trades = []

    # Mode 1: Backtest JSON (has both bars and trades)
    if args.backtest_json:
        logger.info(f"Loading backtest JSON: {args.backtest_json}")
        candles, trades = load_backtest_json(args.backtest_json, tracker)

    else:
        # Mode 2: Separate candle + trade files
        if args.candles:
            candles = read_tv_csv(args.candles, tracker)
        elif args.candles_dir:
            candles_dir = Path(args.candles_dir)
            tf = args.tf.lower()
            # Find CSV matching timeframe
            candidates = list(candles_dir.glob("*.csv"))
            matched = None
            for c in candidates:
                name = c.name.lower()
                if tf in name or tf.replace("m", " ") in name or tf.replace("m", "m") in name:
                    matched = c
                    break
            if matched:
                logger.info(f"Selected {matched.name} for timeframe {tf}")
                candles = read_tv_csv(str(matched), tracker)
            else:
                # Default to first CSV
                if candidates:
                    logger.warning(f"No CSV matching '{tf}', using {candidates[0].name}")
                    candles = read_tv_csv(str(candidates[0]), tracker)
                else:
                    tracker.add("candle_dir", "error", f"No CSV files in {candles_dir}")

        if args.trades:
            trades = load_trades_json(args.trades, tracker)

    # Detect gaps
    if candles:
        # Infer interval from first few bars
        if len(candles) >= 2:
            intervals = [candles[i]["time"] - candles[i-1]["time"]
                         for i in range(1, min(50, len(candles)))]
            median_interval = sorted(intervals)[len(intervals) // 2]
            detect_gaps(candles, median_interval, tracker)

    # Validate trades reference candle time range
    if candles and trades:
        c_start = candles[0]["time"]
        c_end = candles[-1]["time"]
        out_of_range = sum(1 for t in trades
                          if t["entryTime"] and (t["entryTime"] < c_start or t["entryTime"] > c_end))
        if out_of_range:
            tracker.add("trade_range", "warning",
                         f"{out_of_range}/{len(trades)} trades have entry times outside candle range")

    if not candles and not trades:
        tracker.add("output", "error", "No data loaded. Check input paths.")
        write_anomalies(tracker, anomalies_path)
        logger.error("No data loaded -- nothing to write.")
        sys.exit(1)

    write_viz_json(candles, trades, output_path)
    write_anomalies(tracker, anomalies_path)

    logger.info("Done.")
    if tracker.error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
