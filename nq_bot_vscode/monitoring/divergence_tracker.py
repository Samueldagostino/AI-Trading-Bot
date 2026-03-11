"""
Divergence Tracker
===================
Tracks and categorizes divergences between paper and live trading
instances running on the same market data feed.

Categories:
  - SIGNAL_MISMATCH: different signal generated
  - DIRECTION_MISMATCH: same signal, different direction (CRITICAL)
  - SCORE_DRIFT: score differs by > 0.05
  - FILL_SLIPPAGE: live fill > 2 ticks worse than paper
  - TIMING_DIVERGENCE: live execution > 200ms after paper
  - MISSING_TRADE: paper took trade but live didn't (or vice versa)
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DivergenceCategory(Enum):
    SIGNAL_MISMATCH = "SIGNAL_MISMATCH"
    DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
    SCORE_DRIFT = "SCORE_DRIFT"
    FILL_SLIPPAGE = "FILL_SLIPPAGE"
    TIMING_DIVERGENCE = "TIMING_DIVERGENCE"
    MISSING_TRADE = "MISSING_TRADE"


class DivergenceSeverity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


# Map category -> default severity
CATEGORY_SEVERITY: Dict[DivergenceCategory, DivergenceSeverity] = {
    DivergenceCategory.SIGNAL_MISMATCH: DivergenceSeverity.WARNING,
    DivergenceCategory.DIRECTION_MISMATCH: DivergenceSeverity.CRITICAL,
    DivergenceCategory.SCORE_DRIFT: DivergenceSeverity.INFO,
    DivergenceCategory.FILL_SLIPPAGE: DivergenceSeverity.WARNING,
    DivergenceCategory.TIMING_DIVERGENCE: DivergenceSeverity.INFO,
    DivergenceCategory.MISSING_TRADE: DivergenceSeverity.CRITICAL,
}


@dataclass
class Divergence:
    """Single divergence event between paper and live."""
    timestamp: datetime
    category: DivergenceCategory
    severity: DivergenceSeverity
    description: str
    paper_value: str = ""
    live_value: str = ""
    bar_index: int = 0
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "category": self.category.value,
            "severity": self.severity.value,
            "description": self.description,
            "paper_value": self.paper_value,
            "live_value": self.live_value,
            "bar_index": self.bar_index,
            "metadata": self.metadata,
        }


@dataclass
class ComparisonResult:
    """Result of comparing paper vs live decisions on one bar."""
    bar_timestamp: datetime
    bar_index: int
    # Signal comparison
    paper_signal: bool = False
    live_signal: bool = False
    paper_direction: str = ""
    live_direction: str = ""
    paper_score: float = 0.0
    live_score: float = 0.0
    paper_hc_pass: bool = False
    live_hc_pass: bool = False
    paper_risk_approved: bool = False
    live_risk_approved: bool = False
    paper_entry: bool = False
    live_entry: bool = False
    # Divergences found
    divergences: List[Divergence] = field(default_factory=list)
    is_clean: bool = True  # No divergences

    def to_dict(self) -> dict:
        return {
            "bar_timestamp": self.bar_timestamp.isoformat(),
            "bar_index": self.bar_index,
            "paper_signal": self.paper_signal,
            "live_signal": self.live_signal,
            "paper_direction": self.paper_direction,
            "live_direction": self.live_direction,
            "paper_score": self.paper_score,
            "live_score": self.live_score,
            "paper_hc_pass": self.paper_hc_pass,
            "live_hc_pass": self.live_hc_pass,
            "paper_entry": self.paper_entry,
            "live_entry": self.live_entry,
            "is_clean": self.is_clean,
            "divergences": [d.to_dict() for d in self.divergences],
        }


@dataclass
class FillComparison:
    """Comparison of paper vs live fill quality."""
    trade_id: str
    direction: str
    # Entry
    paper_entry_price: float = 0.0
    live_entry_price: float = 0.0
    entry_slippage_pts: float = 0.0
    # Timing
    paper_fill_time: Optional[datetime] = None
    live_fill_time: Optional[datetime] = None
    fill_latency_ms: float = 0.0
    # Stop
    paper_stop: float = 0.0
    live_stop: float = 0.0
    stop_match: bool = True
    # Exit
    paper_exit_price: float = 0.0
    live_exit_price: float = 0.0
    exit_slippage_pts: float = 0.0
    # PnL
    paper_pnl: float = 0.0
    live_pnl: float = 0.0
    pnl_delta: float = 0.0

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "direction": self.direction,
            "paper_entry_price": self.paper_entry_price,
            "live_entry_price": self.live_entry_price,
            "entry_slippage_pts": round(self.entry_slippage_pts, 4),
            "paper_fill_time": self.paper_fill_time.isoformat() if self.paper_fill_time else None,
            "live_fill_time": self.live_fill_time.isoformat() if self.live_fill_time else None,
            "fill_latency_ms": round(self.fill_latency_ms, 1),
            "paper_stop": self.paper_stop,
            "live_stop": self.live_stop,
            "stop_match": self.stop_match,
            "paper_exit_price": self.paper_exit_price,
            "live_exit_price": self.live_exit_price,
            "exit_slippage_pts": round(self.exit_slippage_pts, 4),
            "paper_pnl": round(self.paper_pnl, 2),
            "live_pnl": round(self.live_pnl, 2),
            "pnl_delta": round(self.pnl_delta, 2),
        }


class DivergenceTracker:
    """
    Tracks all divergences across a paper-vs-live comparison session.

    Provides categorized divergence counts, alerts on critical events,
    and produces daily summary reports.
    """

    # Thresholds
    SCORE_DRIFT_THRESHOLD = 0.05   # Flag if score differs by > 0.05
    FILL_SLIPPAGE_TICKS = 2        # Flag if live fill > 2 ticks worse
    TIMING_THRESHOLD_MS = 200.0    # Flag if live > 200ms after paper

    def __init__(self, alert_manager=None, log_path: Optional[str] = None):
        """
        Args:
            alert_manager: Optional AlertManager for critical divergence alerts
            log_path: Optional path for JSONL divergence log
        """
        self._alert_manager = alert_manager
        self._divergences: List[Divergence] = []
        self._comparisons: List[ComparisonResult] = []
        self._fill_comparisons: List[FillComparison] = []
        self._log_path = Path(log_path) if log_path else None
        self._bars_compared = 0

        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def compare_decisions(
        self,
        bar_timestamp: datetime,
        bar_index: int,
        paper_result: Optional[dict],
        live_result: Optional[dict],
    ) -> ComparisonResult:
        """
        Compare decisions from paper and live instances on the same bar.

        Args:
            bar_timestamp: Timestamp of the bar
            bar_index: Index of the bar in the session
            paper_result: Return value from paper orchestrator.process_bar()
            live_result: Return value from live orchestrator.process_bar()

        Returns:
            ComparisonResult with any divergences flagged
        """
        self._bars_compared += 1

        result = ComparisonResult(
            bar_timestamp=bar_timestamp,
            bar_index=bar_index,
        )

        # Extract signals from results
        paper_has_entry = paper_result is not None and paper_result.get("action") == "entry"
        live_has_entry = live_result is not None and live_result.get("action") == "entry"

        result.paper_signal = paper_result is not None
        result.live_signal = live_result is not None
        result.paper_entry = paper_has_entry
        result.live_entry = live_has_entry

        if paper_has_entry:
            result.paper_direction = paper_result.get("direction", "")
            result.paper_score = paper_result.get("signal_score", 0.0)
            result.paper_hc_pass = True
            result.paper_risk_approved = True

        if live_has_entry:
            result.live_direction = live_result.get("direction", "")
            result.live_score = live_result.get("signal_score", 0.0)
            result.live_hc_pass = True
            result.live_risk_approved = True

        # --- Check divergences ---

        # MISSING_TRADE: one entered, the other didn't
        if paper_has_entry and not live_has_entry:
            div = Divergence(
                timestamp=bar_timestamp,
                category=DivergenceCategory.MISSING_TRADE,
                severity=DivergenceSeverity.CRITICAL,
                description="Paper entered trade but live did not",
                paper_value=f"entry {result.paper_direction}",
                live_value="no entry",
                bar_index=bar_index,
            )
            result.divergences.append(div)

        elif live_has_entry and not paper_has_entry:
            div = Divergence(
                timestamp=bar_timestamp,
                category=DivergenceCategory.MISSING_TRADE,
                severity=DivergenceSeverity.CRITICAL,
                description="Live entered trade but paper did not",
                paper_value="no entry",
                live_value=f"entry {result.live_direction}",
                bar_index=bar_index,
            )
            result.divergences.append(div)

        # Both entered — check direction and score
        elif paper_has_entry and live_has_entry:
            # DIRECTION_MISMATCH
            if result.paper_direction != result.live_direction:
                div = Divergence(
                    timestamp=bar_timestamp,
                    category=DivergenceCategory.DIRECTION_MISMATCH,
                    severity=DivergenceSeverity.CRITICAL,
                    description="Paper and live entered in different directions",
                    paper_value=result.paper_direction,
                    live_value=result.live_direction,
                    bar_index=bar_index,
                )
                result.divergences.append(div)

            # SCORE_DRIFT
            score_delta = abs(result.paper_score - result.live_score)
            if score_delta > self.SCORE_DRIFT_THRESHOLD:
                div = Divergence(
                    timestamp=bar_timestamp,
                    category=DivergenceCategory.SCORE_DRIFT,
                    severity=DivergenceSeverity.INFO,
                    description=f"Signal score drift: {score_delta:.4f}",
                    paper_value=f"{result.paper_score:.4f}",
                    live_value=f"{result.live_score:.4f}",
                    bar_index=bar_index,
                )
                result.divergences.append(div)

        # Both produced results but different signal presence
        elif (paper_result is not None) != (live_result is not None):
            div = Divergence(
                timestamp=bar_timestamp,
                category=DivergenceCategory.SIGNAL_MISMATCH,
                severity=DivergenceSeverity.WARNING,
                description="Signal presence differs between paper and live",
                paper_value="signal" if paper_result else "no signal",
                live_value="signal" if live_result else "no signal",
                bar_index=bar_index,
            )
            result.divergences.append(div)

        # Track divergences
        if result.divergences:
            result.is_clean = False
            for div in result.divergences:
                self._divergences.append(div)
                self._log_divergence(div)
                if div.severity == DivergenceSeverity.CRITICAL:
                    self._alert_critical(div)

        self._comparisons.append(result)
        return result

    def compare_fills(
        self,
        trade_id: str,
        direction: str,
        paper_entry_price: float,
        live_entry_price: float,
        paper_fill_time: Optional[datetime] = None,
        live_fill_time: Optional[datetime] = None,
        paper_stop: float = 0.0,
        live_stop: float = 0.0,
        paper_exit_price: float = 0.0,
        live_exit_price: float = 0.0,
        paper_pnl: float = 0.0,
        live_pnl: float = 0.0,
    ) -> FillComparison:
        """
        Compare fill quality between paper and live for a matched trade.

        Returns:
            FillComparison with slippage and latency metrics.
        """
        # Slippage: how much worse is live vs paper (signed)
        if direction == "long":
            entry_slippage = live_entry_price - paper_entry_price
            exit_slippage = paper_exit_price - live_exit_price
        else:
            entry_slippage = paper_entry_price - live_entry_price
            exit_slippage = live_exit_price - paper_exit_price

        # Timing
        latency_ms = 0.0
        if paper_fill_time and live_fill_time:
            delta = (live_fill_time - paper_fill_time).total_seconds() * 1000
            latency_ms = max(delta, 0.0)

        fc = FillComparison(
            trade_id=trade_id,
            direction=direction,
            paper_entry_price=paper_entry_price,
            live_entry_price=live_entry_price,
            entry_slippage_pts=entry_slippage,
            paper_fill_time=paper_fill_time,
            live_fill_time=live_fill_time,
            fill_latency_ms=latency_ms,
            paper_stop=paper_stop,
            live_stop=live_stop,
            stop_match=abs(paper_stop - live_stop) < 0.01,
            paper_exit_price=paper_exit_price,
            live_exit_price=live_exit_price,
            exit_slippage_pts=exit_slippage,
            paper_pnl=paper_pnl,
            live_pnl=live_pnl,
            pnl_delta=live_pnl - paper_pnl,
        )
        self._fill_comparisons.append(fc)

        # Check for divergence conditions
        # FILL_SLIPPAGE: > 2 ticks (0.50 pts) worse
        tick_size = 0.25
        if abs(entry_slippage) > self.FILL_SLIPPAGE_TICKS * tick_size:
            div = Divergence(
                timestamp=paper_fill_time or datetime.now(timezone.utc),
                category=DivergenceCategory.FILL_SLIPPAGE,
                severity=DivergenceSeverity.WARNING,
                description=f"Entry slippage {entry_slippage:.2f}pts exceeds {self.FILL_SLIPPAGE_TICKS} ticks",
                paper_value=f"{paper_entry_price:.2f}",
                live_value=f"{live_entry_price:.2f}",
                metadata={"trade_id": trade_id, "slippage_pts": entry_slippage},
            )
            self._divergences.append(div)
            self._log_divergence(div)

        # TIMING_DIVERGENCE: > 200ms
        if latency_ms > self.TIMING_THRESHOLD_MS:
            div = Divergence(
                timestamp=paper_fill_time or datetime.now(timezone.utc),
                category=DivergenceCategory.TIMING_DIVERGENCE,
                severity=DivergenceSeverity.INFO,
                description=f"Fill latency {latency_ms:.0f}ms exceeds {self.TIMING_THRESHOLD_MS}ms",
                paper_value=paper_fill_time.isoformat() if paper_fill_time else "",
                live_value=live_fill_time.isoformat() if live_fill_time else "",
                metadata={"trade_id": trade_id, "latency_ms": latency_ms},
            )
            self._divergences.append(div)
            self._log_divergence(div)

        return fc

    def get_summary(self) -> dict:
        """Generate session summary statistics."""
        by_category = {}
        by_severity = {}
        for d in self._divergences:
            by_category[d.category.value] = by_category.get(d.category.value, 0) + 1
            by_severity[d.severity.value] = by_severity.get(d.severity.value, 0) + 1

        clean_bars = sum(1 for c in self._comparisons if c.is_clean)
        total_bars = len(self._comparisons)

        # Fill stats
        entry_slippages = [fc.entry_slippage_pts for fc in self._fill_comparisons]
        avg_slippage = sum(entry_slippages) / len(entry_slippages) if entry_slippages else 0.0
        latencies = [fc.fill_latency_ms for fc in self._fill_comparisons]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        # PnL correlation (simple)
        paper_pnls = [fc.paper_pnl for fc in self._fill_comparisons if fc.paper_pnl != 0]
        live_pnls = [fc.live_pnl for fc in self._fill_comparisons if fc.live_pnl != 0]

        return {
            "bars_compared": self._bars_compared,
            "total_comparisons": total_bars,
            "clean_bars": clean_bars,
            "agreement_rate": round(clean_bars / total_bars * 100, 2) if total_bars > 0 else 100.0,
            "total_divergences": len(self._divergences),
            "by_category": by_category,
            "by_severity": by_severity,
            "total_fill_comparisons": len(self._fill_comparisons),
            "avg_entry_slippage_pts": round(avg_slippage, 4),
            "avg_fill_latency_ms": round(avg_latency, 1),
        }

    def get_verdict(self) -> dict:
        """
        Pass/fail verdict for scaling up.

        Pass criteria:
          - Signal agreement rate >= 99%
          - No DIRECTION_MISMATCH events
          - Average slippage <= 2 ticks (0.50 pts)
          - No MISSING_TRADE events
          - PnL correlation >= 0.95
        """
        summary = self.get_summary()
        by_cat = summary["by_category"]

        signal_agreement = summary["agreement_rate"] >= 99.0
        no_direction_mismatch = by_cat.get("DIRECTION_MISMATCH", 0) == 0
        avg_slip_ok = abs(summary["avg_entry_slippage_pts"]) <= 0.50
        no_missing_trades = by_cat.get("MISSING_TRADE", 0) == 0

        # PnL correlation (simplified: check if correlation is above threshold)
        pnl_corr = self._compute_pnl_correlation()
        pnl_corr_ok = pnl_corr >= 0.95 if pnl_corr is not None else True  # Pass if no fills yet

        all_pass = all([signal_agreement, no_direction_mismatch, avg_slip_ok,
                        no_missing_trades, pnl_corr_ok])

        reasons = []
        if not signal_agreement:
            reasons.append(f"Signal agreement {summary['agreement_rate']:.1f}% < 99%")
        if not no_direction_mismatch:
            reasons.append(f"DIRECTION_MISMATCH: {by_cat.get('DIRECTION_MISMATCH', 0)} events")
        if not avg_slip_ok:
            reasons.append(f"Avg slippage {summary['avg_entry_slippage_pts']:.3f}pts > 0.50pts")
        if not no_missing_trades:
            reasons.append(f"MISSING_TRADE: {by_cat.get('MISSING_TRADE', 0)} events")
        if not pnl_corr_ok:
            reasons.append(f"PnL correlation {pnl_corr:.3f} < 0.95")

        return {
            "verdict": "PASS" if all_pass else "FAIL",
            "checks": {
                "signal_agreement": signal_agreement,
                "no_direction_mismatch": no_direction_mismatch,
                "avg_slippage_ok": avg_slip_ok,
                "no_missing_trades": no_missing_trades,
                "pnl_correlation_ok": pnl_corr_ok,
            },
            "details": {
                "agreement_rate": summary["agreement_rate"],
                "direction_mismatches": by_cat.get("DIRECTION_MISMATCH", 0),
                "avg_slippage_pts": summary["avg_entry_slippage_pts"],
                "missing_trades": by_cat.get("MISSING_TRADE", 0),
                "pnl_correlation": pnl_corr,
            },
            "fail_reasons": reasons,
        }

    @property
    def divergences(self) -> List[Divergence]:
        return list(self._divergences)

    @property
    def comparisons(self) -> List[ComparisonResult]:
        return list(self._comparisons)

    @property
    def fill_comparisons(self) -> List[FillComparison]:
        return list(self._fill_comparisons)

    def save_log(self, path: str) -> None:
        """Save full comparison log to JSON."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "summary": self.get_summary(),
            "verdict": self.get_verdict(),
            "comparisons": [c.to_dict() for c in self._comparisons],
            "fill_comparisons": [fc.to_dict() for fc in self._fill_comparisons],
            "divergences": [d.to_dict() for d in self._divergences],
        }
        with open(p, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Comparison log saved to %s", p)

    def _compute_pnl_correlation(self) -> Optional[float]:
        """Compute Pearson correlation between paper and live PnL."""
        if len(self._fill_comparisons) < 3:
            return None
        paper = [fc.paper_pnl for fc in self._fill_comparisons]
        live = [fc.live_pnl for fc in self._fill_comparisons]

        n = len(paper)
        mean_p = sum(paper) / n
        mean_l = sum(live) / n

        cov = sum((paper[i] - mean_p) * (live[i] - mean_l) for i in range(n))
        std_p = (sum((p - mean_p) ** 2 for p in paper)) ** 0.5
        std_l = (sum((l - mean_l) ** 2 for l in live)) ** 0.5

        if std_p == 0 or std_l == 0:
            return 1.0 if std_p == std_l == 0 else 0.0

        return round(cov / (std_p * std_l), 4)

    def _log_divergence(self, div: Divergence) -> None:
        """Log divergence to file and console."""
        level = {
            DivergenceSeverity.INFO: logging.INFO,
            DivergenceSeverity.WARNING: logging.WARNING,
            DivergenceSeverity.CRITICAL: logging.CRITICAL,
        }.get(div.severity, logging.INFO)

        logger.log(
            level,
            "DIVERGENCE [%s] %s: %s (paper=%s, live=%s)",
            div.severity.value, div.category.value,
            div.description, div.paper_value, div.live_value,
        )

        if self._log_path:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(div.to_dict()) + "\n")

    def _alert_critical(self, div: Divergence) -> None:
        """Send alert for CRITICAL divergences via AlertManager."""
        if self._alert_manager is None:
            return
        try:
            from monitoring.alerting import Alert, AlertSeverity
            alert = Alert(
                event_type=f"divergence_{div.category.value.lower()}",
                severity=AlertSeverity.CRITICAL,
                title=f"Paper-Live Divergence: {div.category.value}",
                message=div.description,
                data={
                    "paper_value": div.paper_value,
                    "live_value": div.live_value,
                    "bar_index": div.bar_index,
                },
            )
            self._alert_manager.enqueue(alert)
        except Exception as e:
            logger.error("Failed to send divergence alert: %s", e)
