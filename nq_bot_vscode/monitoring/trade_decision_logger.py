"""
Trade Decision Logger -- Full Approval + Rejection Logging
============================================================
Logs EVERY signal evaluation -- both approved and rejected trades --
with complete reasoning chains.  READ-ONLY: observes and records
decisions, never modifies them.

Output files:
  logs/trade_decisions.json          -- machine-readable, one JSON per line
  logs/trade_decisions_readable.txt  -- human-readable formatted summaries
  logs/daily_summaries.txt           -- end-of-day session statistics
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TradeDecisionLogger:
    """
    Logs every trade decision (approved or rejected) with full context.

    This logger is READ-ONLY -- it observes and records decisions made by
    the trading pipeline.  It never modifies trade logic, sizing, or gates.

    Usage:
        tdl = TradeDecisionLogger("logs")
        tdl.log_rejection(
            price_at_signal=21450.25,
            signal_direction="SHORT",
            rejection_stage="HTF_GATE",
            rejection_details={...},
        )
        tdl.log_approval(
            price_at_signal=21450.25,
            signal_direction="LONG",
            confluence_score=82,
            ...
        )
        summary = tdl.get_session_summary()
    """

    def __init__(self, log_dir: str = "logs"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._json_path = self._log_dir / "trade_decisions.json"
        self._readable_path = self._log_dir / "trade_decisions_readable.txt"
        self._daily_summary_path = self._log_dir / "daily_summaries.txt"

        # Session counters
        self._decisions: List[Dict[str, Any]] = []
        self._approved_count = 0
        self._rejected_count = 0
        self._rejection_by_stage: Dict[str, int] = {}
        self._rejection_reasons: Dict[str, int] = {}

    # ================================================================
    # REJECTION LOGGING
    # ================================================================
    def log_rejection(
        self,
        price_at_signal: float,
        signal_direction: str,
        rejection_stage: str,
        rejection_details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Log a rejected trade signal with full reasoning.

        Args:
            price_at_signal: Price when signal was evaluated.
            signal_direction: "LONG" or "SHORT".
            rejection_stage: One of HTF_GATE, CONFLUENCE, MODIFIER_STANDSIDE,
                             SAFETY_RAIL, HC_SCORE, HC_STOP, MIN_RR, REGIME,
                             NAN_GUARD, RISK_REJECT.
            rejection_details: Dict with rejection context (htf_biases,
                               confluence_score, modifier_values, etc.).

        Returns:
            The complete logged entry dict.
        """
        details = rejection_details or {}

        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": "REJECTED",
            "price_at_signal": price_at_signal,
            "signal_direction": signal_direction.upper(),
            "rejection_stage": rejection_stage,
            "rejection_details": {
                "htf_biases": details.get("htf_biases", {}),
                "conflicting_timeframes": details.get("conflicting_timeframes", []),
                "confluence_score": details.get("confluence_score"),
                "confluence_threshold": details.get("confluence_threshold", 0.75),
                "modifier_values": details.get("modifier_values", {
                    "overnight": 1.0,
                    "fomc": 1.0,
                    "gamma": 1.0,
                    "volatility": 1.0,
                    "total": 1.0,
                }),
                "stand_aside_reason": details.get("stand_aside_reason"),
                "safety_rail_triggered": details.get("safety_rail_triggered"),
            },
            "what_would_have_happened": None,
        }

        self._write_json(entry)
        self._write_readable_rejection(entry)
        self._decisions.append(entry)
        self._rejected_count += 1

        # Track rejection stage breakdown
        self._rejection_by_stage[rejection_stage] = (
            self._rejection_by_stage.get(rejection_stage, 0) + 1
        )

        # Track reason string
        reason_str = details.get("stand_aside_reason") or rejection_stage
        self._rejection_reasons[reason_str] = (
            self._rejection_reasons.get(reason_str, 0) + 1
        )

        logger.debug(
            "Decision logged: REJECTED %s @ %.2f -- stage=%s",
            signal_direction, price_at_signal, rejection_stage,
        )

        return entry

    # ================================================================
    # APPROVAL LOGGING
    # ================================================================
    def log_approval(
        self,
        price_at_signal: float,
        signal_direction: str,
        confluence_score: float,
        modifier_values: Optional[Dict[str, float]] = None,
        position_size: float = 2.0,
        stop_width: float = 0.0,
        runner_trail_width: float = 0.0,
        entry_price: float = 0.0,
        c1_target: float = 0.0,
        c2_trail_start: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Log an approved trade with full modifier chain and sizing details.

        Args:
            price_at_signal: Price when signal was evaluated.
            signal_direction: "LONG" or "SHORT".
            confluence_score: Final confluence/HC score.
            modifier_values: Dict with overnight/fomc/gamma/volatility/total.
            position_size: Number of contracts.
            stop_width: Stop distance in points.
            runner_trail_width: C2 runner trail width in points.
            entry_price: Actual entry price.
            c1_target: C1 target/trail trigger level.
            c2_trail_start: C2 trail start level.

        Returns:
            The complete logged entry dict.
        """
        mods = modifier_values or {
            "overnight": 1.0,
            "fomc": 1.0,
            "gamma": 1.0,
            "volatility": 1.0,
            "total": 1.0,
        }

        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": "APPROVED",
            "price_at_signal": price_at_signal,
            "signal_direction": signal_direction.upper(),
            "confluence_score": confluence_score,
            "modifier_values": mods,
            "position_size": position_size,
            "stop_width": stop_width,
            "runner_trail_width": runner_trail_width,
            "entry_price": entry_price,
            "c1_target": c1_target,
            "c2_trail_start": c2_trail_start,
        }

        self._write_json(entry)
        self._write_readable_approval(entry)
        self._decisions.append(entry)
        self._approved_count += 1

        logger.debug(
            "Decision logged: APPROVED %s @ %.2f -- score=%.3f size=%.0f",
            signal_direction, price_at_signal, confluence_score, position_size,
        )

        return entry

    # ================================================================
    # EXIT LOGGING
    # ================================================================
    def log_exit(
        self,
        direction: str,
        entry_price: float,
        exit_price: float,
        total_pnl: float,
        exit_reason: str = "",
    ) -> Dict[str, Any]:
        """
        Log a trade exit with realized P&L.

        Args:
            direction: "LONG" or "SHORT".
            entry_price: Original entry price.
            exit_price: Exit price.
            total_pnl: Realized P&L in dollars.
            exit_reason: Why the trade closed.

        Returns:
            The complete logged entry dict.
        """
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": "EXIT",
            "signal_direction": (direction or "UNKNOWN").upper(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "total_pnl": total_pnl,
            "exit_reason": exit_reason,
        }

        self._write_json(entry)
        self._decisions.append(entry)

        logger.debug(
            "Decision logged: EXIT %s entry=%.2f exit=%.2f pnl=%.2f reason=%s",
            direction, entry_price, exit_price, total_pnl, exit_reason,
        )

        return entry

    # ================================================================
    # SESSION SUMMARY
    # ================================================================
    def get_session_summary(self) -> Dict[str, Any]:
        """
        Return session statistics.

        Returns dict with:
            total_signals, approved, rejected, rejection_breakdown_by_stage,
            approval_rate, most_common_rejection_reason
        """
        total = self._approved_count + self._rejected_count
        approval_rate = (
            (self._approved_count / total * 100) if total > 0 else 0.0
        )

        most_common_reason = ""
        if self._rejection_reasons:
            most_common_reason = max(
                self._rejection_reasons, key=self._rejection_reasons.get,
            )

        return {
            "total_signals": total,
            "approved": self._approved_count,
            "rejected": self._rejected_count,
            "rejection_breakdown_by_stage": dict(self._rejection_by_stage),
            "approval_rate": round(approval_rate, 2),
            "most_common_rejection_reason": most_common_reason,
        }

    def write_daily_summary(self) -> None:
        """Append session summary to daily_summaries.txt and print to terminal."""
        summary = self.get_session_summary()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = [
            f"\n{'=' * 50}",
            f"  SESSION SUMMARY -- {now_str}",
            f"{'=' * 50}",
            f"  Total signals evaluated:  {summary['total_signals']}",
            f"  Approved:                 {summary['approved']}",
            f"  Rejected:                 {summary['rejected']}",
            f"  Approval rate:            {summary['approval_rate']:.1f}%",
            f"  Most common rejection:    {summary['most_common_rejection_reason']}",
            f"",
            f"  Rejection breakdown by stage:",
        ]
        for stage, count in sorted(
            summary["rejection_breakdown_by_stage"].items(),
            key=lambda x: x[1], reverse=True,
        ):
            lines.append(f"    {stage:.<30} {count}")
        lines.append(f"{'=' * 50}\n")

        text = "\n".join(lines)

        # Print to terminal
        print(text)

        # Append to daily summaries file
        try:
            with open(self._daily_summary_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            logger.warning("Failed to write daily summary: %s", e)

    # ================================================================
    # INTERNAL -- FILE I/O
    # ================================================================
    def _write_json(self, entry: Dict[str, Any]) -> None:
        """Append one JSON object per line to trade_decisions.json."""
        try:
            with open(self._json_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write trade decision JSON: %s", e)

    def _write_readable_rejection(self, entry: Dict[str, Any]) -> None:
        """Append human-readable rejection to trade_decisions_readable.txt."""
        ts = entry["timestamp"]
        direction = entry["signal_direction"]
        price = entry["price_at_signal"]
        stage = entry["rejection_stage"]
        details = entry["rejection_details"]

        # Build reason string
        reason = details.get("stand_aside_reason") or stage

        # Format HTF biases if available
        htf_biases = details.get("htf_biases", {})
        if htf_biases:
            bias_parts = []
            for tf in ["1D", "4H", "1H", "30m", "15m", "5m"]:
                b = htf_biases.get(tf, "N/A")
                short = b[:4].upper() if b else "N/A"
                bias_parts.append(f"{tf}={short}")
            htf_str = " ".join(bias_parts)
        else:
            htf_str = "not available"

        # Format confluence
        conf_score = details.get("confluence_score")
        if conf_score is not None:
            conf_str = f"{conf_score}"
        else:
            conf_str = "not evaluated (gate rejected first)"

        # Format modifiers
        mods = details.get("modifier_values", {})
        if any(v != 0.0 for v in mods.values()):
            mod_parts = [f"{k}={v:.2f}" for k, v in mods.items()]
            mod_str = " ".join(mod_parts)
        else:
            mod_str = "not evaluated"

        lines = [
            f"=== [REJECTED] {ts} === {direction} @ {price:,.2f}",
            f" Stage: {stage}",
            f" Reason: {reason}",
            f" HTF Biases: {htf_str}",
            f" Confluence: {conf_str}",
            f" Modifiers: {mod_str}",
            "=" * 50,
            "",
        ]

        try:
            with open(self._readable_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            logger.warning("Failed to write readable rejection: %s", e)

    def _write_readable_approval(self, entry: Dict[str, Any]) -> None:
        """Append human-readable approval to trade_decisions_readable.txt."""
        ts = entry["timestamp"]
        direction = entry["signal_direction"]
        price = entry["price_at_signal"]
        score = entry["confluence_score"]
        mods = entry.get("modifier_values", {})
        size = entry["position_size"]
        stop = entry["stop_width"]
        entry_px = entry["entry_price"]

        mod_parts = [f"{k}={v:.2f}" for k, v in mods.items()]
        mod_str = " ".join(mod_parts) if mod_parts else "none"

        lines = [
            f"=== [APPROVED] {ts} === {direction} @ {price:,.2f}",
            f" Score: {score:.3f}",
            f" Size: {size:.0f} contracts",
            f" Entry: {entry_px:,.2f}  Stop: {stop:.1f}pts",
            f" Modifiers: {mod_str}",
            "=" * 50,
            "",
        ]

        try:
            with open(self._readable_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            logger.warning("Failed to write readable approval: %s", e)

    # ================================================================
    # READING (for review_decisions.py)
    # ================================================================
    def read_all_decisions(self) -> List[Dict[str, Any]]:
        """Read all decisions from trade_decisions.json."""
        entries = []
        if not self._json_path.exists():
            return entries
        try:
            with open(self._json_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError as e:
            logger.warning("Failed to read trade decisions: %s", e)
        return entries

    def reset_session(self) -> None:
        """Reset session counters (call at start of new day)."""
        self._decisions.clear()
        self._approved_count = 0
        self._rejected_count = 0
        self._rejection_by_stage.clear()
        self._rejection_reasons.clear()
