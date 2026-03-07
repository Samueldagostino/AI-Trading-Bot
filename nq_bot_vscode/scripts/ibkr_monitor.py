"""
IBKR Paper Trading Monitor
=============================
Validates live performance against backtest expectations.

Reads logs/ibkr_trades.json and logs/ibkr_decisions.json,
computes running statistics, compares to backtest baseline,
and raises alerts when performance degrades.

Features:
  - Running stats updated after every trade
  - Comparison to backtest baseline (config/backtest_baseline.json)
  - Z-score significance testing (WR chi-squared, PF bootstrap)
  - Three-tier alerts: GREEN / YELLOW / RED
  - Rolling 20-trade PF for early degradation warning
  - Weekly report generation (C1/C2 breakdown, Z-scores, 4-week trend)
  - Auto-trigger on Friday RTH close or on-demand via --weekly-report

Usage:
    python scripts/ibkr_monitor.py                # Live tail mode
    python scripts/ibkr_monitor.py --snapshot      # One-time snapshot
    python scripts/ibkr_monitor.py --weekly-report # Generate weekly report

Reads from:
    logs/ibkr_trades.json
    logs/ibkr_decisions.json
    config/backtest_baseline.json

Writes to:
    logs/weekly_report_{date}.json
    docs/viz_data.json
"""

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ── Project path setup ──
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

LOGS_DIR = project_dir / "logs"
DOCS_DIR = project_dir / "docs"
TRADES_LOG = LOGS_DIR / "ibkr_trades.json"
DECISIONS_LOG = LOGS_DIR / "ibkr_decisions.json"
BASELINE_PATH = project_dir / "config" / "backtest_baseline.json"
VIZ_DATA_PATH = DOCS_DIR / "viz_data.json"

# MNQ point value — matches Broker/order_executor.py
MNQ_POINT_VALUE = 2.0

# RTH close hour in ET (16:00)
RTH_CLOSE_HOUR = 16
RTH_CLOSE_MINUTE = 0


# ═══════════════════════════════════════════════════════════════
# ALERT LEVELS
# ═══════════════════════════════════════════════════════════════

class AlertLevel(Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class Alert:
    level: AlertLevel
    category: str
    message: str


# ═══════════════════════════════════════════════════════════════
# BASELINE
# ═══════════════════════════════════════════════════════════════

@dataclass
class BacktestBaseline:
    """Backtest baseline metrics loaded from JSON."""
    profit_factor: float = 1.73
    win_rate_pct: float = 61.9
    trades_per_month: int = 254
    expectancy_per_trade: float = 16.79
    max_drawdown_pct: float = 1.4
    total_pnl: float = 25581.0
    c1_pnl: float = 10008.0
    c2_pnl: float = 15573.0
    total_trades: int = 1524
    months: int = 6
    account_size: float = 50000.0

    @classmethod
    def from_json(cls, path: Path) -> "BacktestBaseline":
        if not path.exists():
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
            return cls(
                profit_factor=data.get("profit_factor", 1.73),
                win_rate_pct=data.get("win_rate_pct", 61.9),
                trades_per_month=data.get("trades_per_month", 254),
                expectancy_per_trade=data.get("expectancy_per_trade", 16.79),
                max_drawdown_pct=data.get("max_drawdown_pct", 1.4),
                total_pnl=data.get("total_pnl", 25581.0),
                c1_pnl=data.get("c1_pnl", 10008.0),
                c2_pnl=data.get("c2_pnl", 15573.0),
                total_trades=data.get("total_trades", 1524),
                months=data.get("months", 6),
                account_size=data.get("account_size", 50000.0),
            )
        except (json.JSONDecodeError, IOError):
            return cls()


# ═══════════════════════════════════════════════════════════════
# TRADE RECORD — parsed from log entries
# ═══════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """A single completed trade extracted from the logs."""
    timestamp: str
    direction: str
    pnl: float
    c1_pnl: float = 0.0
    c2_pnl: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    contracts: int = 2
    source: str = ""  # "signal", "sweep", "confluence"


# ═══════════════════════════════════════════════════════════════
# STATISTICS ENGINE
# ═══════════════════════════════════════════════════════════════

class StatsEngine:
    """
    Computes all running statistics from trade records.

    Pure computation — no I/O, no side effects.
    """

    def __init__(self, baseline: BacktestBaseline):
        self.baseline = baseline
        self.trades: List[TradeRecord] = []
        self.decisions: List[Dict] = []

    # ── Core metrics ──

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl < 0)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def cumulative_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def gross_profit(self) -> float:
        return sum(t.pnl for t in self.trades if t.pnl > 0)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.pnl for t in self.trades if t.pnl < 0))

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def c1_pnl(self) -> float:
        return sum(t.c1_pnl for t in self.trades)

    @property
    def c2_pnl(self) -> float:
        return sum(t.c2_pnl for t in self.trades)

    @property
    def avg_winner_pts(self) -> float:
        winners = [t.pnl for t in self.trades if t.pnl > 0]
        if not winners:
            return 0.0
        return (sum(winners) / len(winners)) / MNQ_POINT_VALUE

    @property
    def avg_loser_pts(self) -> float:
        losers = [t.pnl for t in self.trades if t.pnl < 0]
        if not losers:
            return 0.0
        return abs(sum(losers) / len(losers)) / MNQ_POINT_VALUE

    # ── Rolling 20-trade PF ──

    @property
    def rolling_pf(self) -> float:
        """Profit factor over the last 20 trades."""
        window = self.trades[-20:]
        if not window:
            return 0.0
        gross_p = sum(t.pnl for t in window if t.pnl > 0)
        gross_l = abs(sum(t.pnl for t in window if t.pnl < 0))
        if gross_l == 0:
            return float("inf") if gross_p > 0 else 0.0
        return gross_p / gross_l

    # ── Max consecutive losses ──

    @property
    def max_consecutive_losses(self) -> int:
        max_run = 0
        current_run = 0
        for t in self.trades:
            if t.pnl < 0:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 0
        return max_run

    # ── Drawdown ──

    def current_drawdown_pct(self, account_size: float) -> float:
        """Current drawdown as percentage of account."""
        if not self.trades or account_size <= 0:
            return 0.0
        equity_curve = []
        running = 0.0
        for t in self.trades:
            running += t.pnl
            equity_curve.append(running)
        peak = 0.0
        max_dd = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = peak - eq
            max_dd = max(max_dd, dd)
        return (max_dd / account_size) * 100

    def max_drawdown_pct(self, account_size: float) -> float:
        """Same as current_drawdown_pct — max DD over full history."""
        return self.current_drawdown_pct(account_size)

    # ── Daily stats ──

    @property
    def daily_pnl(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(
            t.pnl for t in self.trades
            if t.timestamp[:10] == today
        )

    @property
    def daily_trades(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(
            1 for t in self.trades
            if t.timestamp[:10] == today
        )

    # ── Block rates (from decisions log) ──

    @property
    def hc_block_count(self) -> int:
        return sum(
            1 for d in self.decisions
            if d.get("event") == "bar_processed"
        )

    @property
    def htf_block_count(self) -> int:
        """Count HTF gate blocks from decisions (bars with no signal)."""
        return sum(
            1 for d in self.decisions
            if d.get("event") == "bar_processed"
            and d.get("htf_consensus") in ("neutral", "n/a")
        )

    # ── Trades per day rate ──

    @property
    def trades_per_day(self) -> float:
        if not self.trades:
            return 0.0
        dates = {t.timestamp[:10] for t in self.trades if t.timestamp}
        if not dates:
            return 0.0
        return self.total_trades / len(dates)

    # ── Backtest comparison ──

    def wr_z_score(self) -> float:
        """
        Z-score comparing paper win rate to backtest win rate.

        Uses normal approximation to binomial (chi-squared equivalent
        for two-outcome test): z = (p_obs - p_exp) / sqrt(p_exp * (1-p_exp) / n)

        Positive z = outperforming backtest.
        Negative z = underperforming.
        |z| > 1.96 = significant at 95%.
        |z| > 2.58 = significant at 99%.
        """
        n = self.total_trades
        if n == 0:
            return 0.0
        p_obs = self.wins / n
        p_exp = self.baseline.win_rate_pct / 100
        if p_exp <= 0 or p_exp >= 1:
            return 0.0
        se = math.sqrt(p_exp * (1 - p_exp) / n)
        if se == 0:
            return 0.0
        return (p_obs - p_exp) / se

    def pf_z_score(self) -> float:
        """
        Z-score for profit factor using log-ratio method.

        PF isn't normally distributed, so we use ln(PF) which is
        approximately normal. Standard error of ln(PF) is estimated
        from the sample: se ≈ sqrt(1/wins + 1/losses).

        Returns z = (ln(PF_obs) - ln(PF_exp)) / se.
        """
        if self.wins == 0 or self.losses == 0:
            return 0.0
        pf_obs = self.profit_factor
        pf_exp = self.baseline.profit_factor
        if pf_obs <= 0 or pf_exp <= 0:
            return 0.0
        ln_diff = math.log(pf_obs) - math.log(pf_exp)
        se = math.sqrt(1.0 / self.wins + 1.0 / self.losses)
        if se == 0:
            return 0.0
        return ln_diff / se

    def is_wr_significant(self) -> bool:
        """True if win rate difference is statistically significant (p < 0.05)."""
        return abs(self.wr_z_score()) > 1.96

    def is_pf_significant(self) -> bool:
        """True if PF difference is statistically significant (p < 0.05)."""
        return abs(self.pf_z_score()) > 1.96


# ═══════════════════════════════════════════════════════════════
# ALERT ENGINE
# ═══════════════════════════════════════════════════════════════

class AlertEngine:
    """
    Evaluates alert thresholds against running stats.

    RED:    Stop trading, investigate.
    YELLOW: Monitor closely.
    GREEN:  System performing as expected.
    """

    # ── RED thresholds ──
    RED_PF_THRESHOLD = 0.8
    RED_PF_MIN_TRADES = 50
    RED_WR_THRESHOLD = 45.0
    RED_WR_MIN_TRADES = 50
    RED_MAX_DD_PCT = 3.0
    RED_MAX_CONSEC_LOSSES = 10
    RED_DAILY_LOSS_LIMIT = 500.0

    # ── YELLOW thresholds ──
    YELLOW_PF_LOW = 0.8
    YELLOW_PF_HIGH = 1.2
    YELLOW_PF_MIN_TRADES = 30
    YELLOW_WR_LOW = 45.0
    YELLOW_WR_HIGH = 55.0
    YELLOW_MAX_DD_PCT = 2.0
    YELLOW_MAX_CONSEC_LOSSES = 6

    # ── GREEN thresholds ──
    GREEN_PF_MIN = 1.2
    GREEN_WR_MIN = 55.0
    GREEN_MAX_DD_PCT = 1.5

    def evaluate(self, stats: StatsEngine) -> List[Alert]:
        """Run all alert checks, return list of active alerts."""
        alerts: List[Alert] = []
        account = stats.baseline.account_size

        # ── RED checks ──
        if (stats.total_trades >= self.RED_PF_MIN_TRADES
                and stats.profit_factor < self.RED_PF_THRESHOLD):
            alerts.append(Alert(
                AlertLevel.RED, "PROFIT FACTOR",
                f"PF {stats.profit_factor:.2f} < {self.RED_PF_THRESHOLD} "
                f"after {stats.total_trades} trades",
            ))

        if (stats.total_trades >= self.RED_WR_MIN_TRADES
                and stats.win_rate < self.RED_WR_THRESHOLD):
            alerts.append(Alert(
                AlertLevel.RED, "WIN RATE",
                f"WR {stats.win_rate:.1f}% < {self.RED_WR_THRESHOLD}% "
                f"after {stats.total_trades} trades",
            ))

        dd = stats.max_drawdown_pct(account)
        if dd > self.RED_MAX_DD_PCT:
            alerts.append(Alert(
                AlertLevel.RED, "DRAWDOWN",
                f"DD {dd:.1f}% exceeds {self.RED_MAX_DD_PCT}%",
            ))

        consec = stats.max_consecutive_losses
        if consec >= self.RED_MAX_CONSEC_LOSSES:
            alerts.append(Alert(
                AlertLevel.RED, "CONSEC LOSSES",
                f"{consec} consecutive losses (limit: {self.RED_MAX_CONSEC_LOSSES})",
            ))

        if abs(stats.daily_pnl) >= self.RED_DAILY_LOSS_LIMIT and stats.daily_pnl < 0:
            alerts.append(Alert(
                AlertLevel.RED, "DAILY LOSS",
                f"Daily loss ${stats.daily_pnl:.2f} exceeds -${self.RED_DAILY_LOSS_LIMIT:.0f}",
            ))

        # ── YELLOW checks (only if no RED for same category) ──
        red_cats = {a.category for a in alerts}

        if ("PROFIT FACTOR" not in red_cats
                and stats.total_trades >= self.YELLOW_PF_MIN_TRADES
                and self.YELLOW_PF_LOW <= stats.profit_factor < self.YELLOW_PF_HIGH):
            alerts.append(Alert(
                AlertLevel.YELLOW, "PROFIT FACTOR",
                f"PF {stats.profit_factor:.2f} in warning range "
                f"[{self.YELLOW_PF_LOW}-{self.YELLOW_PF_HIGH}] "
                f"after {stats.total_trades} trades",
            ))

        if ("WIN RATE" not in red_cats
                and stats.total_trades >= self.YELLOW_PF_MIN_TRADES
                and self.YELLOW_WR_LOW <= stats.win_rate < self.YELLOW_WR_HIGH):
            alerts.append(Alert(
                AlertLevel.YELLOW, "WIN RATE",
                f"WR {stats.win_rate:.1f}% in warning range "
                f"[{self.YELLOW_WR_LOW}-{self.YELLOW_WR_HIGH}%]",
            ))

        if ("DRAWDOWN" not in red_cats
                and dd > self.YELLOW_MAX_DD_PCT):
            alerts.append(Alert(
                AlertLevel.YELLOW, "DRAWDOWN",
                f"DD {dd:.1f}% exceeds {self.YELLOW_MAX_DD_PCT}%",
            ))

        if ("CONSEC LOSSES" not in red_cats
                and consec >= self.YELLOW_MAX_CONSEC_LOSSES):
            alerts.append(Alert(
                AlertLevel.YELLOW, "CONSEC LOSSES",
                f"{consec} consecutive losses (warning: {self.YELLOW_MAX_CONSEC_LOSSES})",
            ))

        return alerts

    def overall_status(self, stats: StatsEngine) -> AlertLevel:
        """Determine overall system health."""
        alerts = self.evaluate(stats)
        if any(a.level == AlertLevel.RED for a in alerts):
            return AlertLevel.RED
        if any(a.level == AlertLevel.YELLOW for a in alerts):
            return AlertLevel.YELLOW
        return AlertLevel.GREEN


# ═══════════════════════════════════════════════════════════════
# WEEKLY REPORT
# ═══════════════════════════════════════════════════════════════

@dataclass
class WeeklyReport:
    """Summary of one trading week."""
    week_start: str  # ISO date (Monday)
    week_end: str    # ISO date (Friday)
    trade_count: int = 0
    net_pnl: float = 0.0
    profit_factor: float = 0.0
    win_rate_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    c1_pnl: float = 0.0
    c2_pnl: float = 0.0
    c1_pnl_pts: float = 0.0
    c2_pnl_pts: float = 0.0
    wins: int = 0
    losses: int = 0
    avg_winner_pts: float = 0.0
    avg_loser_pts: float = 0.0
    max_consecutive_losses: int = 0

    # Backtest comparison
    wr_z_score: float = 0.0
    pf_z_score: float = 0.0
    wr_significant: bool = False
    pf_significant: bool = False

    def to_dict(self) -> Dict:
        """Serialize to JSON-safe dict."""
        return {
            "week_start": self.week_start,
            "week_end": self.week_end,
            "trade_count": self.trade_count,
            "net_pnl": round(self.net_pnl, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor < 100 else None,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "c1_pnl": round(self.c1_pnl, 2),
            "c2_pnl": round(self.c2_pnl, 2),
            "c1_pnl_pts": round(self.c1_pnl_pts, 1),
            "c2_pnl_pts": round(self.c2_pnl_pts, 1),
            "wins": self.wins,
            "losses": self.losses,
            "avg_winner_pts": round(self.avg_winner_pts, 1),
            "avg_loser_pts": round(self.avg_loser_pts, 1),
            "max_consecutive_losses": self.max_consecutive_losses,
            "wr_z_score": round(self.wr_z_score, 3),
            "pf_z_score": round(self.pf_z_score, 3),
            "wr_significant": self.wr_significant,
            "pf_significant": self.pf_significant,
        }


def _week_boundaries(date_str: str) -> Tuple[str, str]:
    """Return (monday, friday) ISO date strings for the week containing date_str."""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    friday = monday + timedelta(days=4)
    return monday.strftime("%Y-%m-%d"), friday.strftime("%Y-%m-%d")


def _trades_in_range(
    trades: List[TradeRecord], start: str, end: str
) -> List[TradeRecord]:
    """Filter trades whose timestamp falls within [start, end] inclusive."""
    return [
        t for t in trades
        if start <= t.timestamp[:10] <= end
    ]


def generate_weekly_report(
    trades: List[TradeRecord],
    baseline: BacktestBaseline,
    week_date: Optional[str] = None,
) -> WeeklyReport:
    """
    Generate a weekly report for the week containing week_date.

    If week_date is None, uses the current date.
    """
    if week_date is None:
        week_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_start, week_end = _week_boundaries(week_date)

    week_trades = _trades_in_range(trades, week_start, week_end)

    # Build a StatsEngine for just this week's trades
    stats = StatsEngine(baseline)
    stats.trades = week_trades

    report = WeeklyReport(
        week_start=week_start,
        week_end=week_end,
        trade_count=stats.total_trades,
        net_pnl=stats.cumulative_pnl,
        profit_factor=stats.profit_factor,
        win_rate_pct=stats.win_rate,
        max_drawdown_pct=stats.max_drawdown_pct(baseline.account_size),
        c1_pnl=stats.c1_pnl,
        c2_pnl=stats.c2_pnl,
        c1_pnl_pts=stats.c1_pnl / MNQ_POINT_VALUE if MNQ_POINT_VALUE else 0,
        c2_pnl_pts=stats.c2_pnl / MNQ_POINT_VALUE if MNQ_POINT_VALUE else 0,
        wins=stats.wins,
        losses=stats.losses,
        avg_winner_pts=stats.avg_winner_pts,
        avg_loser_pts=stats.avg_loser_pts,
        max_consecutive_losses=stats.max_consecutive_losses,
        wr_z_score=stats.wr_z_score(),
        pf_z_score=stats.pf_z_score(),
        wr_significant=stats.is_wr_significant(),
        pf_significant=stats.is_pf_significant(),
    )
    return report


def compute_weekly_reports(
    trades: List[TradeRecord],
    baseline: BacktestBaseline,
) -> List[WeeklyReport]:
    """Generate reports for every week that has trades."""
    if not trades:
        return []

    # Find all unique weeks
    weeks_seen: Dict[str, str] = {}  # monday -> friday
    for t in trades:
        if not t.timestamp:
            continue
        monday, friday = _week_boundaries(t.timestamp)
        weeks_seen[monday] = friday

    # Sort by week start
    sorted_weeks = sorted(weeks_seen.items())

    reports = []
    for monday, friday in sorted_weeks:
        report = generate_weekly_report(trades, baseline, monday)
        reports.append(report)
    return reports


# ── Trend computation ──

TREND_IMPROVING = "improving"
TREND_DEGRADING = "degrading"
TREND_STABLE = "stable"


def compute_4_week_trend(reports: List[WeeklyReport]) -> Dict:
    """
    Compute rolling 4-week trend from weekly reports.

    Compares most recent 2 weeks vs prior 2 weeks.
    Returns trend direction for PF, WR, and PnL.
    """
    if len(reports) < 4:
        return {
            "status": "insufficient_data",
            "weeks_available": len(reports),
            "pf_trend": TREND_STABLE,
            "wr_trend": TREND_STABLE,
            "pnl_trend": TREND_STABLE,
        }

    recent = reports[-4:]
    prior_2 = recent[:2]
    latest_2 = recent[2:]

    def _avg(values: List[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    prior_pf = _avg([r.profit_factor for r in prior_2 if r.profit_factor < 100])
    latest_pf = _avg([r.profit_factor for r in latest_2 if r.profit_factor < 100])
    prior_wr = _avg([r.win_rate_pct for r in prior_2])
    latest_wr = _avg([r.win_rate_pct for r in latest_2])
    prior_pnl = sum(r.net_pnl for r in prior_2)
    latest_pnl = sum(r.net_pnl for r in latest_2)

    def _trend(latest_val: float, prior_val: float, threshold: float) -> str:
        if prior_val == 0:
            return TREND_STABLE
        pct_change = (latest_val - prior_val) / abs(prior_val) if prior_val != 0 else 0
        if pct_change > threshold:
            return TREND_IMPROVING
        elif pct_change < -threshold:
            return TREND_DEGRADING
        return TREND_STABLE

    # 10% change threshold for PF/WR, 20% for PnL
    pf_trend = _trend(latest_pf, prior_pf, 0.10)
    wr_trend = _trend(latest_wr, prior_wr, 0.10)
    pnl_trend = _trend(latest_pnl, prior_pnl, 0.20)

    return {
        "status": "computed",
        "weeks_available": len(reports),
        "prior_2_weeks": {
            "avg_pf": round(prior_pf, 2),
            "avg_wr": round(prior_wr, 1),
            "total_pnl": round(prior_pnl, 2),
        },
        "latest_2_weeks": {
            "avg_pf": round(latest_pf, 2),
            "avg_wr": round(latest_wr, 1),
            "total_pnl": round(latest_pnl, 2),
        },
        "pf_trend": pf_trend,
        "wr_trend": wr_trend,
        "pnl_trend": pnl_trend,
    }


# ── Export functions ──

def export_weekly_report(report: WeeklyReport, output_dir: Path = LOGS_DIR) -> Path:
    """Export a weekly report to logs/weekly_report_{date}.json."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"weekly_report_{report.week_end}.json"
    path = output_dir / filename
    with open(path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)
    return path


def update_viz_data(
    reports: List[WeeklyReport],
    trend: Dict,
    viz_path: Path = VIZ_DATA_PATH,
) -> Path:
    """
    Update docs/viz_data.json for GitHub Pages dashboard.

    Merges weekly reports into existing viz_data if present.
    """
    viz_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing data
    existing: Dict = {}
    if viz_path.exists():
        try:
            with open(viz_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = {}

    # Build weekly_reports array
    weekly_data = [r.to_dict() for r in reports]

    existing["weekly_reports"] = weekly_data
    existing["trend"] = trend
    existing["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(viz_path, "w") as f:
        json.dump(existing, f, indent=2)
    return viz_path


def is_friday_rth_close() -> bool:
    """Check if current time is Friday at/after RTH close (16:00 ET)."""
    et_now = datetime.now(ZoneInfo("America/New_York"))
    return (
        et_now.weekday() == 4  # Friday
        and et_now.hour >= RTH_CLOSE_HOUR
    )


def run_weekly_report(
    trades_path: Path = TRADES_LOG,
    baseline_path: Path = BASELINE_PATH,
) -> Optional[WeeklyReport]:
    """Generate, export, and print the weekly report."""
    baseline = BacktestBaseline.from_json(baseline_path)
    raw_trades = load_json(trades_path)
    trades = parse_trades(raw_trades)

    if not trades:
        print("No trades found — cannot generate weekly report.")
        return None

    # Generate report for the current week
    report = generate_weekly_report(trades, baseline)

    # Export to JSON
    export_path = export_weekly_report(report)

    # Compute all weekly reports for trend analysis
    all_reports = compute_weekly_reports(trades, baseline)
    trend = compute_4_week_trend(all_reports)

    # Update viz_data.json
    viz_path = update_viz_data(all_reports, trend)

    # Print summary
    print(_render_weekly_report(report, trend, baseline))
    print(f"\n  Exported to: {export_path}")
    print(f"  Viz data:    {viz_path}")

    return report


def _render_weekly_report(
    report: WeeklyReport,
    trend: Dict,
    baseline: BacktestBaseline,
) -> str:
    """Render the weekly report as a terminal string."""
    lines: List[str] = []
    W = 62
    bar = "=" * W

    lines.append("")
    lines.append(bar)
    lines.append(
        f"  WEEKLY REPORT  {report.week_start} -> {report.week_end}"
    )
    lines.append(bar)

    # Summary
    lines.append("")
    lines.append("  SUMMARY")
    lines.append("  " + "-" * 58)
    pf_str = f"{report.profit_factor:.2f}" if report.profit_factor < 100 else "inf"
    lines.append(f"  Trades:          {report.trade_count}  ({report.wins}W / {report.losses}L)")
    lines.append(f"  Net PnL:         ${report.net_pnl:+.2f}")
    lines.append(f"  Win Rate:        {report.win_rate_pct:.1f}%")
    lines.append(f"  Profit Factor:   {pf_str}")
    lines.append(f"  Max DD:          {report.max_drawdown_pct:.2f}%")
    lines.append(f"  Avg Winner:      +{report.avg_winner_pts:.1f} pts")
    lines.append(f"  Avg Loser:       -{report.avg_loser_pts:.1f} pts")
    lines.append(f"  Max Consec Loss: {report.max_consecutive_losses}")

    # C1/C2 breakdown
    lines.append("")
    lines.append("  C1/C2 BREAKDOWN")
    lines.append("  " + "-" * 58)
    lines.append(f"  C1 PnL:  ${report.c1_pnl:+.2f}  ({report.c1_pnl_pts:+.1f} pts)")
    lines.append(f"  C2 PnL:  ${report.c2_pnl:+.2f}  ({report.c2_pnl_pts:+.1f} pts)")
    c1_pct = (report.c1_pnl / report.net_pnl * 100) if report.net_pnl != 0 else 0
    c2_pct = (report.c2_pnl / report.net_pnl * 100) if report.net_pnl != 0 else 0
    lines.append(f"  C1 share: {c1_pct:.0f}%     C2 share: {c2_pct:.0f}%")

    # Backtest comparison
    lines.append("")
    lines.append("  VS BACKTEST")
    lines.append("  " + "-" * 58)
    wr_sig = " *" if report.wr_significant else ""
    pf_sig = " *" if report.pf_significant else ""
    lines.append(f"  Metric        Paper      Backtest    Z-score")
    lines.append(
        f"  Win Rate      {report.win_rate_pct:5.1f}%"
        f"     {baseline.win_rate_pct:5.1f}%"
        f"       {report.wr_z_score:+.2f}{wr_sig}"
    )
    lines.append(
        f"  Profit Fac    {pf_str:>5}"
        f"      {baseline.profit_factor:5.2f}"
        f"       {report.pf_z_score:+.2f}{pf_sig}"
    )
    if wr_sig or pf_sig:
        lines.append(f"  (* = statistically significant at 95%)")

    # 4-week trend
    lines.append("")
    lines.append("  4-WEEK TREND")
    lines.append("  " + "-" * 58)
    if trend.get("status") == "insufficient_data":
        lines.append(
            f"  Insufficient data ({trend['weeks_available']} weeks, need 4)"
        )
    else:
        pf_t = trend["pf_trend"].upper()
        wr_t = trend["wr_trend"].upper()
        pnl_t = trend["pnl_trend"].upper()
        lines.append(f"  PF trend:   {pf_t}")
        lines.append(f"  WR trend:   {wr_t}")
        lines.append(f"  PnL trend:  {pnl_t}")
        prior = trend["prior_2_weeks"]
        latest = trend["latest_2_weeks"]
        lines.append(
            f"  Prior 2wk:  PF {prior['avg_pf']:.2f}"
            f"  WR {prior['avg_wr']:.1f}%"
            f"  PnL ${prior['total_pnl']:+.2f}"
        )
        lines.append(
            f"  Last 2wk:   PF {latest['avg_pf']:.2f}"
            f"  WR {latest['avg_wr']:.1f}%"
            f"  PnL ${latest['total_pnl']:+.2f}"
        )

    lines.append("")
    lines.append(bar)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# LOG PARSER
# ═══════════════════════════════════════════════════════════════

def load_json(path: Path) -> List[Dict]:
    """Load a JSON log file, returning empty list if missing."""
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def parse_trades(raw: List[Dict]) -> List[TradeRecord]:
    """Extract completed trades from ibkr_trades.json entries."""
    trades = []
    for entry in raw:
        evt = entry.get("event", "")
        if evt == "shutdown_flatten":
            # Flatten events are exits, not full trades with P&L
            continue
        if evt != "fill":
            continue
        # Only count entries that have pnl (closed trades)
        pnl = entry.get("pnl")
        if pnl is None:
            continue
        trades.append(TradeRecord(
            timestamp=entry.get("logged_at", entry.get("timestamp", "")),
            direction=entry.get("direction", ""),
            pnl=float(pnl),
            c1_pnl=float(entry.get("c1_pnl", 0)),
            c2_pnl=float(entry.get("c2_pnl", 0)),
            entry_price=float(entry.get("entry_price", 0)),
            exit_price=float(entry.get("exit_price", 0)),
            contracts=int(entry.get("contracts", 2)),
            source=entry.get("source", ""),
        ))
    return trades


# ═══════════════════════════════════════════════════════════════
# DISPLAY RENDERER
# ═══════════════════════════════════════════════════════════════

def render_dashboard(stats: StatsEngine, alerts: List[Alert]) -> str:
    """Render the full monitoring dashboard as a string."""
    baseline = stats.baseline
    account = baseline.account_size
    lines: List[str] = []
    W = 62
    bar = "=" * W
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Header ──
    status = AlertEngine().overall_status(stats)
    status_label = {
        AlertLevel.GREEN: "GREEN",
        AlertLevel.YELLOW: "** YELLOW **",
        AlertLevel.RED: "*** RED ***",
    }[status]

    lines.append("")
    lines.append(bar)
    lines.append(
        f"  IBKR MONITOR  {now_str}"
        f"  [{status_label}]"
    )
    lines.append(bar)

    # ── Running stats ──
    lines.append("")
    lines.append("  RUNNING STATS")
    lines.append("  " + "-" * 58)

    pf = stats.profit_factor
    pf_str = f"{pf:.2f}" if pf < 100 else "inf"
    rpf = stats.rolling_pf
    rpf_str = f"{rpf:.2f}" if rpf < 100 else "inf"

    lines.append(
        f"  Trades:          {stats.total_trades}"
        f"  ({stats.wins}W / {stats.losses}L)"
    )
    lines.append(
        f"  Cumulative PnL:  ${stats.cumulative_pnl:+.2f}"
    )
    lines.append(
        f"  Win Rate:        {stats.win_rate:.1f}%"
    )
    lines.append(
        f"  Profit Factor:   {pf_str}"
        f"     Rolling 20: {rpf_str}"
    )
    lines.append(
        f"  Avg Winner:      {stats.avg_winner_pts:+.1f} pts"
        f"     Avg Loser: -{stats.avg_loser_pts:.1f} pts"
    )
    lines.append(
        f"  C1 PnL:          ${stats.c1_pnl:+.2f}"
        f"     C2 PnL: ${stats.c2_pnl:+.2f}"
    )
    lines.append(
        f"  Max Consec Loss: {stats.max_consecutive_losses}"
        f"     Drawdown: {stats.max_drawdown_pct(account):.1f}%"
    )
    lines.append(
        f"  Trades/day:      {stats.trades_per_day:.1f}"
        f"     Backtest avg: {baseline.trades_per_month / 21:.1f}/day"
    )

    # ── Backtest comparison (every 10 trades) ──
    if stats.total_trades >= 10 and stats.total_trades % 10 < 10:
        lines.append("")
        lines.append("  BACKTEST COMPARISON")
        lines.append("  " + "-" * 58)

        wr_z = stats.wr_z_score()
        pf_z = stats.pf_z_score()
        wr_sig = "*" if stats.is_wr_significant() else ""
        pf_sig = "*" if stats.is_pf_significant() else ""

        lines.append(
            f"  Metric        Paper      Backtest    Z-score"
        )
        lines.append(
            f"  Win Rate      {stats.win_rate:5.1f}%"
            f"     {baseline.win_rate_pct:5.1f}%"
            f"       {wr_z:+.2f}{wr_sig}"
        )
        lines.append(
            f"  Profit Fac    {pf_str:>5}"
            f"      {baseline.profit_factor:5.2f}"
            f"       {pf_z:+.2f}{pf_sig}"
        )
        if wr_sig or pf_sig:
            lines.append(
                f"  (* = statistically significant at 95%)"
            )

        # Direction of divergence
        if wr_z < -1.96:
            lines.append(
                f"  >> WR significantly BELOW backtest"
            )
        elif wr_z > 1.96:
            lines.append(
                f"  >> WR significantly ABOVE backtest"
            )

    # ── Alerts ──
    lines.append("")
    lines.append("  ALERTS")
    lines.append("  " + "-" * 58)

    if not alerts:
        lines.append("  All systems GREEN")
    else:
        for a in alerts:
            tag = "RED " if a.level == AlertLevel.RED else "WARN"
            lines.append(f"  [{tag}] {a.category}: {a.message}")

    # ── Daily ──
    lines.append("")
    lines.append("  TODAY")
    lines.append("  " + "-" * 58)
    lines.append(
        f"  Trades: {stats.daily_trades}"
        f"     PnL: ${stats.daily_pnl:+.2f}"
    )

    # ── Paper Trading Status Bar ──
    mode = os.getenv("IBKR_ACCOUNT_TYPE", "PAPER").upper()
    # Compute trading day count from first trade
    if stats.trades and len(stats.trades) > 0:
        first_ts = stats.trades[0].get("entry_timestamp", stats.trades[0].get("timestamp", ""))
        if first_ts:
            try:
                first_date = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).date()
                day_count = (datetime.now(timezone.utc).date() - first_date).days + 1
            except (ValueError, TypeError):
                day_count = 1
        else:
            day_count = 1
    else:
        day_count = 0

    pf_bar = f"{pf_str}" if stats.total_trades > 0 else "—"
    wr_bar = f"{stats.win_rate:.0f}%" if stats.total_trades > 0 else "—"
    lines.append("")
    lines.append(
        f"  {mode} | Day {day_count} | {stats.total_trades} trades | "
        f"WR {wr_bar} | PF {pf_bar}"
    )
    lines.append(bar)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINTS
# ═══════════════════════════════════════════════════════════════

def build_stats(
    trades_path: Path = TRADES_LOG,
    decisions_path: Path = DECISIONS_LOG,
    baseline_path: Path = BASELINE_PATH,
) -> StatsEngine:
    """Load logs, parse trades, build stats engine."""
    baseline = BacktestBaseline.from_json(baseline_path)
    stats = StatsEngine(baseline)

    raw_trades = load_json(trades_path)
    stats.trades = parse_trades(raw_trades)

    raw_decisions = load_json(decisions_path)
    stats.decisions = raw_decisions

    return stats


def run_snapshot():
    """One-time snapshot of current state."""
    stats = build_stats()
    alert_engine = AlertEngine()
    alerts = alert_engine.evaluate(stats)
    print(render_dashboard(stats, alerts))


def run_live():
    """Live tail mode — refreshes when log files change."""
    print("IBKR Paper Trading Monitor — Live Mode (Ctrl+C to exit)")
    print(f"Watching: {TRADES_LOG}")
    print(f"          {DECISIONS_LOG}")
    print(f"Baseline: {BASELINE_PATH}")

    trades_mtime: float = 0
    decisions_mtime: float = 0
    weekly_report_generated_today = False

    try:
        while True:
            t_mtime = TRADES_LOG.stat().st_mtime if TRADES_LOG.exists() else 0
            d_mtime = (
                DECISIONS_LOG.stat().st_mtime
                if DECISIONS_LOG.exists() else 0
            )

            if t_mtime != trades_mtime or d_mtime != decisions_mtime:
                trades_mtime = t_mtime
                decisions_mtime = d_mtime

                stats = build_stats()
                alert_engine = AlertEngine()
                alerts = alert_engine.evaluate(stats)

                os.system("clear" if os.name != "nt" else "cls")
                print(render_dashboard(stats, alerts))

            # Friday RTH close auto-trigger for weekly report
            if is_friday_rth_close() and not weekly_report_generated_today:
                weekly_report_generated_today = True
                print("\n  [AUTO] Generating weekly report (Friday RTH close)...")
                run_weekly_report()

            # Reset flag at midnight
            now_utc = datetime.now(timezone.utc)
            if now_utc.hour == 0 and now_utc.minute < 1:
                weekly_report_generated_today = False

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="IBKR Paper Trading Monitor — backtest comparison + alerts"
    )
    parser.add_argument(
        "--snapshot", action="store_true",
        help="One-time snapshot instead of live tail",
    )
    parser.add_argument(
        "--weekly-report", action="store_true",
        help="Generate weekly report and exit",
    )
    args = parser.parse_args()

    if args.weekly_report:
        run_weekly_report()
    elif args.snapshot:
        run_snapshot()
    else:
        run_live()


if __name__ == "__main__":
    main()
