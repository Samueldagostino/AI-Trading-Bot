#!/usr/bin/env python3
"""
Review Trade Decisions -- CLI Tool
====================================
Post-session review of trade decisions from logs/trade_decisions.json.

Commands:
    python review_decisions.py --today       Show today's decisions
    python review_decisions.py --rejections  Show only rejections
    python review_decisions.py --summary     Show statistics
    python review_decisions.py --last N      Show last N decisions

Color coding: green=approved, red=rejected
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

# ── Project path setup ──
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

LOGS_DIR = project_dir / "logs"
DECISIONS_FILE = LOGS_DIR / "trade_decisions.json"

# ── ANSI Colors ──
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"


def load_decisions() -> List[Dict[str, Any]]:
    """Load all decisions from trade_decisions.json."""
    if not DECISIONS_FILE.exists():
        return []

    entries = []
    with open(DECISIONS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def filter_today(decisions: List[Dict]) -> List[Dict]:
    """Filter decisions to today only."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [d for d in decisions if d.get("timestamp", "").startswith(today)]


def filter_rejections(decisions: List[Dict]) -> List[Dict]:
    """Filter to rejected decisions only."""
    return [d for d in decisions if d.get("decision") == "REJECTED"]


def print_decision(entry: Dict, index: int = 0) -> None:
    """Pretty-print a single decision with color coding."""
    decision = entry.get("decision", "UNKNOWN")
    direction = entry.get("signal_direction", "?")
    price = entry.get("price_at_signal", 0)
    ts = entry.get("timestamp", "")

    # Truncate timestamp for display
    ts_short = ts[:19] if len(ts) > 19 else ts

    if decision == "REJECTED":
        color = RED
        stage = entry.get("rejection_stage", "?")
        details = entry.get("rejection_details", {})

        # Header
        print(f"\n  {color}{BOLD}[{index}] REJECTED{RESET} {direction} @ {price:,.2f}  {DIM}{ts_short}{RESET}")
        print(f"      {RED}Stage:{RESET} {stage}")

        # Reason
        reason = details.get("stand_aside_reason") or stage
        print(f"      {RED}Reason:{RESET} {reason}")

        # HTF biases
        htf = details.get("htf_biases", {})
        if htf:
            bias_parts = []
            for tf in ["1D", "4H", "1H", "30m", "15m", "5m"]:
                b = htf.get(tf, "N/A")
                if b and b.upper().startswith("BULL"):
                    bias_parts.append(f"{tf}={GREEN}BULL{RESET}")
                elif b and b.upper().startswith("BEAR"):
                    bias_parts.append(f"{tf}={RED}BEAR{RESET}")
                else:
                    bias_parts.append(f"{tf}={DIM}NEUT{RESET}")
            print(f"      HTF: {' '.join(bias_parts)}")

        # Confluence
        conf = details.get("confluence_score")
        if conf is not None:
            thresh = details.get("confluence_threshold", 0.75)
            print(f"      Confluence: {conf} (threshold: {thresh})")

        # Modifiers
        mods = details.get("modifier_values", {})
        if any(v != 0.0 for v in mods.values()):
            mod_str = "  ".join(f"{k}={v:.2f}" for k, v in mods.items())
            print(f"      Modifiers: {mod_str}")

    elif decision == "APPROVED":
        color = GREEN
        score = entry.get("confluence_score", 0)
        size = entry.get("position_size", 0)
        stop = entry.get("stop_width", 0)
        entry_px = entry.get("entry_price", 0)

        # Header
        print(f"\n  {color}{BOLD}[{index}] APPROVED{RESET} {direction} @ {price:,.2f}  {DIM}{ts_short}{RESET}")
        print(f"      {GREEN}Score:{RESET} {score:.3f}  {GREEN}Size:{RESET} {size:.0f}  {GREEN}Stop:{RESET} {stop:.1f}pts")
        print(f"      {GREEN}Entry:{RESET} {entry_px:,.2f}")

        # Modifiers
        mods = entry.get("modifier_values", {})
        if mods:
            mod_str = "  ".join(f"{k}={v:.2f}" for k, v in mods.items())
            print(f"      Modifiers: {mod_str}")


def print_summary(decisions: List[Dict]) -> None:
    """Print summary statistics."""
    total = len(decisions)
    approved = sum(1 for d in decisions if d.get("decision") == "APPROVED")
    rejected = sum(1 for d in decisions if d.get("decision") == "REJECTED")
    rate = (approved / total * 100) if total > 0 else 0.0

    # Rejection breakdown
    stage_counts: Dict[str, int] = {}
    for d in decisions:
        if d.get("decision") == "REJECTED":
            stage = d.get("rejection_stage", "UNKNOWN")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1

    # Direction breakdown
    long_count = sum(1 for d in decisions if d.get("signal_direction") == "LONG")
    short_count = sum(1 for d in decisions if d.get("signal_direction") == "SHORT")

    print()
    print(f"  {BOLD}{'=' * 50}{RESET}")
    print(f"  {BOLD}TRADE DECISION SUMMARY{RESET}")
    print(f"  {BOLD}{'=' * 50}{RESET}")
    print()
    print(f"  Total signals:    {CYAN}{total}{RESET}")
    print(f"  Approved:         {GREEN}{approved}{RESET}")
    print(f"  Rejected:         {RED}{rejected}{RESET}")
    print(f"  Approval rate:    {YELLOW}{rate:.1f}%{RESET}")
    print()
    print(f"  Direction split:  {GREEN}LONG: {long_count}{RESET}  {RED}SHORT: {short_count}{RESET}")
    print()

    if stage_counts:
        print(f"  {BOLD}Rejection breakdown:{RESET}")
        for stage, count in sorted(stage_counts.items(), key=lambda x: x[1], reverse=True):
            pct = count / rejected * 100 if rejected > 0 else 0
            bar_len = int(pct / 5)  # Scale bar width
            bar = "#" * bar_len
            print(f"    {stage:.<30} {count:>3}  ({pct:5.1f}%)  {DIM}{bar}{RESET}")

    print()
    print(f"  {BOLD}{'=' * 50}{RESET}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Review trade decisions from logs/trade_decisions.json",
    )
    parser.add_argument(
        "--today", action="store_true",
        help="Show only today's decisions",
    )
    parser.add_argument(
        "--rejections", action="store_true",
        help="Show only rejected signals",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Show summary statistics",
    )
    parser.add_argument(
        "--last", type=int, default=0, metavar="N",
        help="Show last N decisions",
    )
    args = parser.parse_args()

    decisions = load_decisions()

    if not decisions:
        print(f"\n  No decisions found in {DECISIONS_FILE}")
        print("  Run the trading bot first to generate decision logs.\n")
        return

    # Apply filters
    if args.today:
        decisions = filter_today(decisions)
        if not decisions:
            print("\n  No decisions found for today.\n")
            return

    if args.rejections:
        decisions = filter_rejections(decisions)
        if not decisions:
            print("\n  No rejections found.\n")
            return

    if args.last > 0:
        decisions = decisions[-args.last:]

    # Display
    if args.summary:
        print_summary(decisions)
    else:
        print(f"\n  {BOLD}Showing {len(decisions)} decision(s):{RESET}")
        for i, d in enumerate(decisions, 1):
            print_decision(d, i)
        print()

        # Always show summary at the bottom
        if len(decisions) > 3:
            print_summary(decisions)


if __name__ == "__main__":
    main()
