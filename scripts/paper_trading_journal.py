"""
Paper Trading Journal
======================
Automated trade journal that captures every paper trade with full context.
Designed to run alongside the IBKR trading runner.

Outputs:
  logs/paper_journal_YYYY-MM-DD.json  — daily trade records
  logs/paper_journal_summary.csv      — daily summary rows

Usage:
  # As a module (imported by the trading runner):
  journal = PaperTradingJournal()
  journal.record_trade(trade_data)

  # Generate summary from existing journal files:
  python scripts/paper_trading_journal.py --summary
"""

import csv
import json
import os
import sys
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent / "nq_bot_vscode"
if not PROJECT_DIR.exists():
    PROJECT_DIR = SCRIPT_DIR.parent

LOGS_DIR = PROJECT_DIR / "logs"


@dataclass
class TradeRecord:
    """Complete trade record for paper trading journal."""
    # Entry
    trade_id: int = 0
    entry_timestamp: str = ""
    entry_price: float = 0.0
    direction: str = ""
    contracts: int = 2
    signal_source: str = ""  # sweep, aggregator, ucl_confirmed
    hc_score: float = 0.0
    htf_bias: str = ""
    htf_strength: float = 0.0
    atr_at_entry: float = 0.0
    session: str = ""  # opening, midday, closing, eth
    stop_distance: float = 0.0
    session_scaled: bool = False

    # C1 leg
    c1_target: float = 0.0
    c1_exit_price: float = 0.0
    c1_exit_reason: str = ""
    c1_pnl: float = 0.0

    # C2 leg
    c2_trail_width: float = 0.0
    c2_exit_price: float = 0.0
    c2_exit_reason: str = ""
    c2_pnl: float = 0.0

    # Overall
    total_pnl: float = 0.0
    duration_bars: int = 0
    duration_minutes: float = 0.0
    mfe_pts: float = 0.0
    mae_pts: float = 0.0

    # Fill quality
    expected_entry_price: float = 0.0
    ibkr_fill_price: float = 0.0
    entry_slippage_pts: float = 0.0
    expected_exit_price: float = 0.0
    ibkr_exit_fill: float = 0.0
    exit_slippage_pts: float = 0.0

    # Regime/context
    regime: str = ""
    modifiers: Dict = field(default_factory=dict)

    # QuantData market context (LOG-ONLY — does not affect scoring)
    market_context: Optional[Dict] = None
    gamma_regime_at_entry: str = "unknown"
    flow_aligned_with_trade: Optional[bool] = None
    favorable_for_momentum: Optional[bool] = None


class PaperTradingJournal:
    """
    Logs every paper trade with full context.
    Outputs: logs/paper_journal_YYYY-MM-DD.json (daily file)
    """

    def __init__(self, logs_dir: Optional[Path] = None):
        self.logs_dir = logs_dir or LOGS_DIR
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._summary_path = self.logs_dir / "paper_journal_summary.csv"

    def _daily_path(self, d: Optional[date] = None) -> Path:
        """Get the daily journal file path."""
        d = d or datetime.now(ET).date()
        return self.logs_dir / f"paper_journal_{d.isoformat()}.json"

    def record_trade(self, trade: TradeRecord) -> None:
        """Append a trade record to today's journal file."""
        path = self._daily_path()

        # Load existing records
        records = []
        if path.exists():
            try:
                records = json.loads(path.read_text())
            except (json.JSONDecodeError, ValueError):
                records = []

        records.append(asdict(trade))

        # Atomic write
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(records, indent=2, default=str))
        tmp_path.rename(path)

    def record_from_dict(self, data: dict) -> None:
        """Create and record a trade from a dictionary."""
        trade = TradeRecord(**{k: v for k, v in data.items()
                               if k in TradeRecord.__dataclass_fields__})
        self.record_trade(trade)

    def get_daily_trades(self, d: Optional[date] = None) -> List[dict]:
        """Load all trades from a specific day."""
        path = self._daily_path(d)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            return []

    def get_all_trades(self) -> List[dict]:
        """Load all trades from all journal files."""
        all_trades = []
        for f in sorted(self.logs_dir.glob("paper_journal_????-??-??.json")):
            try:
                trades = json.loads(f.read_text())
                all_trades.extend(trades)
            except (json.JSONDecodeError, ValueError):
                continue
        return all_trades

    def generate_daily_summary(self, d: Optional[date] = None) -> Optional[dict]:
        """Generate summary metrics for a single day."""
        trades = self.get_daily_trades(d)
        if not trades:
            return None

        d = d or datetime.now(ET).date()
        wins = [t for t in trades if t.get("total_pnl", 0) > 0]
        losses = [t for t in trades if t.get("total_pnl", 0) <= 0]
        total_pnl = sum(t.get("total_pnl", 0) for t in trades)

        slippages = [t.get("entry_slippage_pts", 0) for t in trades
                     if t.get("entry_slippage_pts") is not None]
        avg_slippage = sum(slippages) / len(slippages) if slippages else 0

        # Session breakdown
        sessions = {}
        for t in trades:
            s = t.get("session", "unknown")
            if s not in sessions:
                sessions[s] = 0
            sessions[s] += 1

        # Max drawdown (cumulative)
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cum_pnl += t.get("total_pnl", 0)
            peak = max(peak, cum_pnl)
            dd = peak - cum_pnl
            max_dd = max(max_dd, dd)

        return {
            "date": d.isoformat(),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "pnl": round(total_pnl, 2),
            "avg_slippage": round(avg_slippage, 3),
            "max_dd": round(max_dd, 2),
            "session_breakdown": json.dumps(sessions),
        }

    def append_summary_csv(self, summary: dict) -> None:
        """Append a daily summary row to the CSV file."""
        file_exists = self._summary_path.exists()
        fieldnames = ["date", "trades", "wins", "losses", "pnl",
                       "avg_slippage", "max_dd", "session_breakdown"]

        with open(self._summary_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(summary)

    def print_summary(self):
        """Print a summary of all paper trading to date."""
        all_trades = self.get_all_trades()
        if not all_trades:
            print("No paper trading journal entries found.")
            return

        wins = [t for t in all_trades if t.get("total_pnl", 0) > 0]
        losses = [t for t in all_trades if t.get("total_pnl", 0) <= 0]
        total_pnl = sum(t.get("total_pnl", 0) for t in all_trades)
        gross_win = sum(t.get("total_pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("total_pnl", 0) for t in losses))

        wr = (len(wins) / len(all_trades) * 100) if all_trades else 0
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

        print(f"\n{'=' * 50}")
        print(f"  PAPER TRADING JOURNAL SUMMARY")
        print(f"{'=' * 50}")
        print(f"  Total trades: {len(all_trades)}")
        print(f"  Wins:         {len(wins)}  Losses: {len(losses)}")
        print(f"  Win Rate:     {wr:.1f}%")
        print(f"  Profit Factor:{pf:.2f}")
        print(f"  Total PnL:    ${total_pnl:+.2f}")
        print(f"  Avg PnL:      ${total_pnl / len(all_trades):+.2f}")

        # Daily breakdown
        journal_files = sorted(self.logs_dir.glob("paper_journal_????-??-??.json"))
        if journal_files:
            print(f"\n  Daily Breakdown:")
            for f in journal_files:
                d_str = f.stem.replace("paper_journal_", "")
                day_trades = json.loads(f.read_text())
                day_pnl = sum(t.get("total_pnl", 0) for t in day_trades)
                print(f"    {d_str}: {len(day_trades)} trades, ${day_pnl:+.2f}")

        print(f"{'=' * 50}")


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Journal")
    parser.add_argument("--summary", action="store_true", help="Print full summary")
    parser.add_argument("--generate-csv", action="store_true",
                        help="Generate CSV summary from all journal files")
    args = parser.parse_args()

    journal = PaperTradingJournal()

    if args.generate_csv:
        for f in sorted(LOGS_DIR.glob("paper_journal_????-??-??.json")):
            d_str = f.stem.replace("paper_journal_", "")
            d = date.fromisoformat(d_str)
            summary = journal.generate_daily_summary(d)
            if summary:
                journal.append_summary_csv(summary)
                print(f"  Added: {d_str}")
        print(f"CSV written to: {journal._summary_path}")
    else:
        journal.print_summary()


if __name__ == "__main__":
    main()
