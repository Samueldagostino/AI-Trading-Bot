"""
Execution Analytics Engine
===========================
Tracks fill quality, slippage, latency, and order execution performance.
Non-blocking — all recording methods are fire-and-forget async tasks
that never delay order placement.

Metrics:
  - slippage_ticks: (fill_price - expected_price) in NQ ticks (0.25 per tick)
  - latency_ms: fill_timestamp - order_sent_timestamp
  - fill_rate: % orders resulting in fills vs cancels/rejects
  - partial_fill_rate: % orders with partial fills
  - cost_per_trade: slippage + commission in dollars
"""

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

MNQ_TICK_SIZE = 0.25
MNQ_POINT_VALUE = 2.0
MNQ_COMMISSION_PER_CONTRACT = 1.29


@dataclass
class OrderEvent:
    """Single order lifecycle record."""
    order_id: str
    side: str                       # BUY or SELL
    size: int
    expected_price: float
    order_type: str = "market"      # market, limit, stop
    direction: str = ""             # long_entry, short_entry, long_exit, short_exit
    order_sent_at: Optional[datetime] = None
    fill_price: Optional[float] = None
    fill_size: Optional[int] = None
    fill_at: Optional[datetime] = None
    status: str = "pending"         # pending, filled, partial, cancelled, rejected
    cancel_reason: str = ""
    reject_reason: str = ""
    slippage_ticks: Optional[float] = None
    latency_ms: Optional[int] = None
    commission: float = 0.0


class ExecutionAnalytics:
    """
    Tracks and aggregates order execution quality metrics.

    Thread-safe via asyncio — all DB writes are non-blocking.
    In-memory rolling window for real-time metrics.
    """

    def __init__(self, db_manager=None, rolling_window: int = 20):
        self._db = db_manager
        self._rolling_window = rolling_window
        self._orders: Dict[str, OrderEvent] = {}
        self._completed: Deque[OrderEvent] = deque(maxlen=5000)
        self._rolling: Deque[OrderEvent] = deque(maxlen=rolling_window)

    # ══════════════════════════════════════════════════════════
    # DATA COLLECTION
    # ══════════════════════════════════════════════════════════

    def record_order_sent(
        self,
        order_id: str,
        side: str,
        size: int,
        expected_price: float,
        timestamp: Optional[datetime] = None,
        order_type: str = "market",
        direction: str = "",
    ) -> None:
        """Called when order hits the wire. Non-blocking."""
        ts = timestamp or datetime.now(timezone.utc)
        event = OrderEvent(
            order_id=order_id,
            side=side,
            size=size,
            expected_price=expected_price,
            order_type=order_type,
            direction=direction,
            order_sent_at=ts,
            commission=MNQ_COMMISSION_PER_CONTRACT * size,
        )
        self._orders[order_id] = event
        logger.debug(
            "ANALYTICS order_sent: id=%s side=%s size=%d expected=%.2f type=%s",
            order_id, side, size, expected_price, order_type,
        )

    def record_fill(
        self,
        order_id: str,
        fill_price: float,
        fill_size: int,
        fill_timestamp: Optional[datetime] = None,
    ) -> None:
        """Called when fill comes back. Computes slippage and latency."""
        ts = fill_timestamp or datetime.now(timezone.utc)
        event = self._orders.get(order_id)
        if not event:
            logger.warning("ANALYTICS fill for unknown order_id=%s", order_id)
            # Create a stub event
            event = OrderEvent(
                order_id=order_id,
                side="UNKNOWN",
                size=fill_size,
                expected_price=fill_price,
                order_sent_at=ts,
            )
            self._orders[order_id] = event

        event.fill_price = fill_price
        event.fill_size = fill_size
        event.fill_at = ts

        # Determine status
        if fill_size >= event.size:
            event.status = "filled"
        else:
            event.status = "partial"

        # Compute slippage in ticks
        event.slippage_ticks = self._compute_slippage_ticks(
            event.side, event.expected_price, fill_price
        )

        # Compute latency
        if event.order_sent_at:
            delta = (ts - event.order_sent_at).total_seconds() * 1000
            event.latency_ms = int(delta)

        self._completed.append(event)
        self._rolling.append(event)

        logger.debug(
            "ANALYTICS fill: id=%s price=%.2f slip=%.2f ticks latency=%s ms",
            order_id, fill_price,
            event.slippage_ticks or 0.0,
            event.latency_ms,
        )

        # Fire-and-forget DB persist
        if self._db:
            self._schedule_db_insert(event)

    def record_cancel(
        self,
        order_id: str,
        reason: str = "",
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Called on order cancellation."""
        event = self._orders.get(order_id)
        if not event:
            logger.warning("ANALYTICS cancel for unknown order_id=%s", order_id)
            return
        event.status = "cancelled"
        event.cancel_reason = reason
        self._completed.append(event)

    def record_rejection(
        self,
        order_id: str,
        reason: str = "",
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Called on order rejection."""
        event = self._orders.get(order_id)
        if not event:
            logger.warning("ANALYTICS rejection for unknown order_id=%s", order_id)
            return
        event.status = "rejected"
        event.reject_reason = reason
        self._completed.append(event)

    # ══════════════════════════════════════════════════════════
    # SLIPPAGE COMPUTATION
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _compute_slippage_ticks(
        side: str, expected_price: float, fill_price: float
    ) -> float:
        """
        Slippage in NQ ticks (0.25 per tick).
        Positive = unfavorable slippage (you pay more than expected).

        BUY:  slippage = (fill - expected) / tick_size
        SELL: slippage = (expected - fill) / tick_size
        """
        if side.upper() == "BUY":
            raw = fill_price - expected_price
        else:
            raw = expected_price - fill_price
        return round(raw / MNQ_TICK_SIZE, 2)

    # ══════════════════════════════════════════════════════════
    # METRICS — POINT-IN-TIME
    # ══════════════════════════════════════════════════════════

    def get_rolling_metrics(self) -> Dict[str, Any]:
        """Rolling 20-trade moving averages for all metrics."""
        filled = [e for e in self._rolling if e.status in ("filled", "partial")]
        if not filled:
            return {
                "window": self._rolling_window,
                "count": 0,
                "avg_slippage_ticks": 0.0,
                "avg_latency_ms": 0.0,
                "fill_rate": 0.0,
                "partial_fill_rate": 0.0,
                "avg_cost_per_trade": 0.0,
            }

        all_events = list(self._rolling)
        total = len(all_events)
        fills = [e for e in all_events if e.status == "filled"]
        partials = [e for e in all_events if e.status == "partial"]

        slippages = [e.slippage_ticks for e in filled if e.slippage_ticks is not None]
        latencies = [e.latency_ms for e in filled if e.latency_ms is not None]

        avg_slip = sum(slippages) / len(slippages) if slippages else 0.0
        avg_lat = sum(latencies) / len(latencies) if latencies else 0.0

        # Cost per trade = slippage in dollars + commission
        costs = []
        for e in filled:
            slip_dollars = (e.slippage_ticks or 0) * MNQ_TICK_SIZE * MNQ_POINT_VALUE * e.size
            costs.append(slip_dollars + e.commission)

        return {
            "window": self._rolling_window,
            "count": len(filled),
            "avg_slippage_ticks": round(avg_slip, 2),
            "avg_latency_ms": round(avg_lat, 1),
            "fill_rate": round((len(fills) + len(partials)) / total * 100, 1) if total else 0.0,
            "partial_fill_rate": round(len(partials) / total * 100, 1) if total else 0.0,
            "avg_cost_per_trade": round(sum(costs) / len(costs), 2) if costs else 0.0,
        }

    def get_fill_rate(self) -> float:
        """Overall fill rate as a percentage."""
        all_events = list(self._completed)
        if not all_events:
            return 0.0
        fills = sum(1 for e in all_events if e.status in ("filled", "partial"))
        return round(fills / len(all_events) * 100, 1)

    def get_partial_fill_rate(self) -> float:
        """Partial fill rate as a percentage."""
        all_events = list(self._completed)
        if not all_events:
            return 0.0
        partials = sum(1 for e in all_events if e.status == "partial")
        return round(partials / len(all_events) * 100, 1)

    # ══════════════════════════════════════════════════════════
    # AGGREGATION
    # ══════════════════════════════════════════════════════════

    def get_daily_aggregate(self, target_date: Optional[date] = None) -> Dict[str, Any]:
        """Aggregate metrics for a specific day."""
        target = target_date or date.today()
        events = [
            e for e in self._completed
            if e.order_sent_at and e.order_sent_at.date() == target
        ]
        return self._aggregate_events(events, label=str(target))

    def get_weekly_aggregate(self, week_start: Optional[date] = None) -> Dict[str, Any]:
        """Aggregate metrics for a week starting on the given date."""
        start = week_start or (date.today() - timedelta(days=date.today().weekday()))
        end = start + timedelta(days=7)
        events = [
            e for e in self._completed
            if e.order_sent_at and start <= e.order_sent_at.date() < end
        ]
        return self._aggregate_events(events, label=f"week_{start}")

    def get_time_bucket_aggregates(self) -> Dict[str, Dict[str, Any]]:
        """Aggregate by time-of-day buckets (ET approximation: UTC-5)."""
        buckets = {
            "09:30-10:00": (9, 30, 10, 0),
            "10:00-11:00": (10, 0, 11, 0),
            "11:00-12:00": (11, 0, 12, 0),
            "12:00-13:00": (12, 0, 13, 0),
            "13:00-14:00": (13, 0, 14, 0),
            "14:00-15:00": (14, 0, 15, 0),
            "15:00-16:00": (15, 0, 16, 0),
        }
        result = {}
        for label, (sh, sm, eh, em) in buckets.items():
            # Convert ET hours to UTC (ET = UTC-5 in EST, approximate)
            events = [
                e for e in self._completed
                if e.order_sent_at and self._in_time_bucket(
                    e.order_sent_at, sh + 5, sm, eh + 5, em
                )
            ]
            result[label] = self._aggregate_events(events, label=label)
        return result

    def get_order_type_aggregates(self) -> Dict[str, Dict[str, Any]]:
        """Aggregate by order type (market, limit, stop)."""
        result = {}
        for otype in ("market", "limit", "stop"):
            events = [e for e in self._completed if e.order_type == otype]
            result[otype] = self._aggregate_events(events, label=otype)
        return result

    def get_direction_aggregates(self) -> Dict[str, Dict[str, Any]]:
        """Aggregate by direction (long_entry, short_entry, long_exit, short_exit)."""
        result = {}
        for direction in ("long_entry", "short_entry", "long_exit", "short_exit"):
            events = [e for e in self._completed if e.direction == direction]
            result[direction] = self._aggregate_events(events, label=direction)
        return result

    # ══════════════════════════════════════════════════════════
    # ANOMALY DETECTION
    # ══════════════════════════════════════════════════════════

    def detect_anomalies(self, sigma_threshold: float = 3.0) -> List[OrderEvent]:
        """Find fills with slippage > N standard deviations from mean."""
        filled = [
            e for e in self._completed
            if e.status in ("filled", "partial") and e.slippage_ticks is not None
        ]
        if len(filled) < 5:
            return []

        slippages = [e.slippage_ticks for e in filled]
        mean = sum(slippages) / len(slippages)
        variance = sum((s - mean) ** 2 for s in slippages) / len(slippages)
        std = math.sqrt(variance) if variance > 0 else 0.0

        if std == 0:
            return []

        return [
            e for e in filled
            if abs(e.slippage_ticks - mean) > sigma_threshold * std
        ]

    # ══════════════════════════════════════════════════════════
    # SCALING READINESS
    # ══════════════════════════════════════════════════════════

    def assess_scaling_readiness(
        self, lookback_days: int = 30
    ) -> Dict[str, Any]:
        """
        Assess readiness to increase contract size based on execution quality.

        Uses last N days of data to project slippage at higher sizes.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        recent = [
            e for e in self._completed
            if e.status in ("filled", "partial")
            and e.order_sent_at and e.order_sent_at >= cutoff
            and e.slippage_ticks is not None
        ]

        if len(recent) < 20:
            return {
                "ready": False,
                "reason": f"Insufficient data: {len(recent)} fills in last {lookback_days} days (need 20+)",
                "sample_size": len(recent),
                "projections": {},
            }

        slippages = [e.slippage_ticks for e in recent]
        mean_slip = sum(slippages) / len(slippages)
        max_slip = max(slippages)
        variance = sum((s - mean_slip) ** 2 for s in slippages) / len(slippages)
        std_slip = math.sqrt(variance)

        latencies = [e.latency_ms for e in recent if e.latency_ms is not None]
        mean_latency = sum(latencies) / len(latencies) if latencies else 0

        # Project slippage at higher contract sizes
        # Simple market impact model: slippage grows ~sqrt(size)
        projections = {}
        for multiplier in [2, 4, 8]:
            projected_slip = mean_slip * math.sqrt(multiplier)
            projected_cost = (
                projected_slip * MNQ_TICK_SIZE * MNQ_POINT_VALUE * (2 * multiplier)
                + MNQ_COMMISSION_PER_CONTRACT * (2 * multiplier)
            )
            projections[f"{2 * multiplier}_contracts"] = {
                "projected_slippage_ticks": round(projected_slip, 2),
                "projected_cost_per_trade": round(projected_cost, 2),
            }

        # Verdict logic
        fill_rate = self.get_fill_rate()
        anomalies = self.detect_anomalies()

        ready = True
        reasons = []

        if mean_slip > 4.0:  # > 1 point average slippage
            ready = False
            reasons.append(f"High avg slippage: {mean_slip:.1f} ticks (>{4.0})")

        if std_slip > 3.0:
            ready = False
            reasons.append(f"Volatile slippage: std={std_slip:.1f} ticks (>{3.0})")

        if fill_rate < 95.0:
            ready = False
            reasons.append(f"Low fill rate: {fill_rate:.1f}% (<95%)")

        if mean_latency > 500:
            ready = False
            reasons.append(f"High latency: {mean_latency:.0f}ms (>500ms)")

        if len(anomalies) > len(recent) * 0.05:
            ready = False
            reasons.append(f"Too many anomalous fills: {len(anomalies)}")

        # Determine safe contract count
        safe_contracts = 2  # current baseline
        if ready:
            # Scale up to max where projected slippage < 2x current
            for mult in [2, 4, 8]:
                projected = mean_slip * math.sqrt(mult)
                if projected < mean_slip * 2.5:  # Slippage stays within 2.5x
                    safe_contracts = 2 * mult
                else:
                    break

        return {
            "ready": ready,
            "reason": "; ".join(reasons) if reasons else "Execution quality supports scaling",
            "safe_contracts": safe_contracts,
            "sample_size": len(recent),
            "lookback_days": lookback_days,
            "avg_slippage_ticks": round(mean_slip, 2),
            "std_slippage_ticks": round(std_slip, 2),
            "max_slippage_ticks": round(max_slip, 2),
            "avg_latency_ms": round(mean_latency, 1),
            "fill_rate_pct": fill_rate,
            "anomaly_count": len(anomalies),
            "projections": projections,
        }

    # ══════════════════════════════════════════════════════════
    # EXPORT
    # ══════════════════════════════════════════════════════════

    def export_csv(self, filepath: str) -> int:
        """Export all completed events to CSV. Returns row count."""
        import csv

        events = list(self._completed)
        if not events:
            return 0

        fields = [
            "order_id", "side", "size", "expected_price", "fill_price",
            "fill_size", "slippage_ticks", "latency_ms", "order_type",
            "direction", "status", "commission", "order_sent_at", "fill_at",
            "cancel_reason", "reject_reason",
        ]

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for e in events:
                writer.writerow({
                    "order_id": e.order_id,
                    "side": e.side,
                    "size": e.size,
                    "expected_price": e.expected_price,
                    "fill_price": e.fill_price,
                    "fill_size": e.fill_size,
                    "slippage_ticks": e.slippage_ticks,
                    "latency_ms": e.latency_ms,
                    "order_type": e.order_type,
                    "direction": e.direction,
                    "status": e.status,
                    "commission": e.commission,
                    "order_sent_at": e.order_sent_at.isoformat() if e.order_sent_at else "",
                    "fill_at": e.fill_at.isoformat() if e.fill_at else "",
                    "cancel_reason": e.cancel_reason,
                    "reject_reason": e.reject_reason,
                })
        return len(events)

    # ══════════════════════════════════════════════════════════
    # DB PERSISTENCE
    # ══════════════════════════════════════════════════════════

    async def load_from_db(self, days: int = 30) -> int:
        """Load historical events from database into memory."""
        if not self._db:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            rows = await self._db.fetch(
                """
                SELECT order_id, side, size, expected_price, fill_price,
                       slippage_ticks, latency_ms, order_type, status,
                       order_sent_at, fill_at
                FROM execution_metrics
                WHERE order_sent_at >= $1
                ORDER BY order_sent_at ASC
                """,
                cutoff,
            )
            for row in rows:
                event = OrderEvent(
                    order_id=row["order_id"],
                    side=row["side"],
                    size=row["size"],
                    expected_price=row["expected_price"] or 0.0,
                    fill_price=row["fill_price"],
                    slippage_ticks=row["slippage_ticks"],
                    latency_ms=row["latency_ms"],
                    order_type=row["order_type"] or "market",
                    status=row["status"] or "filled",
                    order_sent_at=row["order_sent_at"],
                    fill_at=row["fill_at"],
                )
                self._completed.append(event)
                if event.status in ("filled", "partial"):
                    self._rolling.append(event)
            return len(rows)
        except Exception as e:
            logger.error("Failed to load execution metrics from DB: %s", e)
            return 0

    def _schedule_db_insert(self, event: OrderEvent) -> None:
        """Fire-and-forget DB insert — never blocks order flow."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._db_insert(event))
        except RuntimeError:
            pass  # No event loop — skip DB write

    async def _db_insert(self, event: OrderEvent) -> None:
        """Insert a single event into the execution_metrics table."""
        if not self._db:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO execution_metrics
                    (order_id, side, size, expected_price, fill_price,
                     slippage_ticks, latency_ms, order_type, status,
                     order_sent_at, fill_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                event.order_id,
                event.side,
                event.size,
                event.expected_price,
                event.fill_price,
                event.slippage_ticks,
                event.latency_ms,
                event.order_type,
                event.status,
                event.order_sent_at,
                event.fill_at,
            )
        except Exception as e:
            logger.error("Failed to insert execution metric: %s", e)

    # ══════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════

    def _aggregate_events(
        self, events: List[OrderEvent], label: str = ""
    ) -> Dict[str, Any]:
        """Compute aggregate stats for a list of events."""
        total = len(events)
        filled = [e for e in events if e.status in ("filled", "partial")]
        cancelled = [e for e in events if e.status == "cancelled"]
        rejected = [e for e in events if e.status == "rejected"]
        partials = [e for e in events if e.status == "partial"]

        slippages = [e.slippage_ticks for e in filled if e.slippage_ticks is not None]
        latencies = [e.latency_ms for e in filled if e.latency_ms is not None]

        avg_slip = sum(slippages) / len(slippages) if slippages else 0.0
        avg_lat = sum(latencies) / len(latencies) if latencies else 0.0

        variance = 0.0
        if len(slippages) > 1:
            variance = sum((s - avg_slip) ** 2 for s in slippages) / len(slippages)
        std_slip = math.sqrt(variance) if variance > 0 else 0.0

        # Costs
        costs = []
        for e in filled:
            slip_dollars = (e.slippage_ticks or 0) * MNQ_TICK_SIZE * MNQ_POINT_VALUE * e.size
            costs.append(slip_dollars + e.commission)
        avg_cost = sum(costs) / len(costs) if costs else 0.0

        return {
            "label": label,
            "total_orders": total,
            "fills": len(filled),
            "cancels": len(cancelled),
            "rejects": len(rejected),
            "partials": len(partials),
            "fill_rate_pct": round(len(filled) / total * 100, 1) if total else 0.0,
            "partial_fill_rate_pct": round(len(partials) / total * 100, 1) if total else 0.0,
            "avg_slippage_ticks": round(avg_slip, 2),
            "std_slippage_ticks": round(std_slip, 2),
            "max_slippage_ticks": round(max(slippages), 2) if slippages else 0.0,
            "min_slippage_ticks": round(min(slippages), 2) if slippages else 0.0,
            "avg_latency_ms": round(avg_lat, 1),
            "avg_cost_per_trade": round(avg_cost, 2),
            "total_cost": round(sum(costs), 2),
        }

    @staticmethod
    def _in_time_bucket(
        ts: datetime, start_hour: int, start_min: int, end_hour: int, end_min: int
    ) -> bool:
        """Check if timestamp falls within a UTC time bucket."""
        t = ts.hour * 60 + ts.minute
        start = start_hour * 60 + start_min
        end = end_hour * 60 + end_min
        return start <= t < end

    def get_all_events(self) -> List[OrderEvent]:
        """Return all completed events."""
        return list(self._completed)

    def get_worst_fills(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return the N worst fills by slippage."""
        filled = [
            e for e in self._completed
            if e.status in ("filled", "partial") and e.slippage_ticks is not None
        ]
        filled.sort(key=lambda e: e.slippage_ticks, reverse=True)
        return [
            {
                "order_id": e.order_id,
                "side": e.side,
                "expected_price": e.expected_price,
                "fill_price": e.fill_price,
                "slippage_ticks": e.slippage_ticks,
                "latency_ms": e.latency_ms,
                "order_type": e.order_type,
                "timestamp": e.order_sent_at.isoformat() if e.order_sent_at else "",
            }
            for e in filled[:n]
        ]
