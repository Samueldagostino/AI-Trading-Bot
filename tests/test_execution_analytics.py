"""
Tests for Execution Analytics Module
======================================
Covers slippage, latency, fill rate, rolling averages,
time bucket aggregation, report generation, scaling readiness,
anomaly detection, and database storage.
"""

import asyncio
import math
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta, date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nq_bot_vscode"))

from monitoring.execution_analytics import (
    ExecutionAnalytics,
    MNQ_TICK_SIZE,
    MNQ_POINT_VALUE,
    MNQ_COMMISSION_PER_CONTRACT,
    OrderEvent,
)
from monitoring.execution_report import ExecutionReport


# ══════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════

@pytest.fixture
def analytics():
    """Fresh ExecutionAnalytics instance with no DB."""
    return ExecutionAnalytics(db_manager=None, rolling_window=20)


@pytest.fixture
def populated_analytics():
    """Analytics with 30 mock trades for aggregation tests."""
    ea = ExecutionAnalytics(db_manager=None, rolling_window=20)
    base_time = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)

    for i in range(30):
        oid = f"ORDER-{i:04d}"
        side = "BUY" if i % 2 == 0 else "SELL"
        expected = 21000.0 + i * 0.25
        slippage = (i % 5) * 0.25  # 0, 0.25, 0.50, 0.75, 1.0 pts
        if side == "BUY":
            fill = expected + slippage
        else:
            fill = expected - slippage
        ts_sent = base_time + timedelta(minutes=i * 5)
        ts_fill = ts_sent + timedelta(milliseconds=50 + i * 10)

        ea.record_order_sent(
            order_id=oid, side=side, size=1,
            expected_price=expected, timestamp=ts_sent,
            order_type="market" if i % 3 != 0 else "limit",
            direction="long_entry" if side == "BUY" else "short_entry",
        )
        ea.record_fill(
            order_id=oid, fill_price=fill,
            fill_size=1, fill_timestamp=ts_fill,
        )

    return ea


# ══════════════════════════════════════════════════════════
# SLIPPAGE TESTS
# ══════════════════════════════════════════════════════════

class TestSlippageCalculation:
    def test_slippage_calculation_buy(self, analytics):
        """BUY slippage: (fill - expected) / tick_size. Positive = unfavorable."""
        analytics.record_order_sent(
            order_id="BUY-001", side="BUY", size=1,
            expected_price=21000.00,
        )
        analytics.record_fill(
            order_id="BUY-001", fill_price=21000.50, fill_size=1,
        )
        event = analytics._orders["BUY-001"]
        # (21000.50 - 21000.00) / 0.25 = 2.0 ticks
        assert event.slippage_ticks == 2.0

    def test_slippage_calculation_sell(self, analytics):
        """SELL slippage: (expected - fill) / tick_size. Positive = unfavorable."""
        analytics.record_order_sent(
            order_id="SELL-001", side="SELL", size=1,
            expected_price=21000.00,
        )
        analytics.record_fill(
            order_id="SELL-001", fill_price=20999.50, fill_size=1,
        )
        event = analytics._orders["SELL-001"]
        # (21000.00 - 20999.50) / 0.25 = 2.0 ticks
        assert event.slippage_ticks == 2.0

    def test_slippage_zero_when_perfect_fill(self, analytics):
        """No slippage when fill = expected."""
        analytics.record_order_sent(
            order_id="PERF-001", side="BUY", size=1,
            expected_price=21000.00,
        )
        analytics.record_fill(
            order_id="PERF-001", fill_price=21000.00, fill_size=1,
        )
        event = analytics._orders["PERF-001"]
        assert event.slippage_ticks == 0.0

    def test_slippage_negative_favorable_buy(self, analytics):
        """BUY filled below expected = negative slippage (favorable)."""
        analytics.record_order_sent(
            order_id="FAV-001", side="BUY", size=1,
            expected_price=21000.00,
        )
        analytics.record_fill(
            order_id="FAV-001", fill_price=20999.75, fill_size=1,
        )
        event = analytics._orders["FAV-001"]
        # (20999.75 - 21000.00) / 0.25 = -1.0 tick
        assert event.slippage_ticks == -1.0


# ══════════════════════════════════════════════════════════
# LATENCY TESTS
# ══════════════════════════════════════════════════════════

class TestLatencyCalculation:
    def test_latency_calculation(self, analytics):
        """Latency = fill_timestamp - order_sent_timestamp in ms."""
        t0 = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(milliseconds=150)

        analytics.record_order_sent(
            order_id="LAT-001", side="BUY", size=1,
            expected_price=21000.00, timestamp=t0,
        )
        analytics.record_fill(
            order_id="LAT-001", fill_price=21000.25,
            fill_size=1, fill_timestamp=t1,
        )
        event = analytics._orders["LAT-001"]
        assert event.latency_ms == 150

    def test_latency_zero(self, analytics):
        """Zero latency when sent and filled at same instant."""
        t0 = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)
        analytics.record_order_sent(
            order_id="LAT-002", side="BUY", size=1,
            expected_price=21000.00, timestamp=t0,
        )
        analytics.record_fill(
            order_id="LAT-002", fill_price=21000.00,
            fill_size=1, fill_timestamp=t0,
        )
        event = analytics._orders["LAT-002"]
        assert event.latency_ms == 0


# ══════════════════════════════════════════════════════════
# FILL RATE TESTS
# ══════════════════════════════════════════════════════════

class TestFillRate:
    def test_fill_rate(self, analytics):
        """Fill rate = fills / (fills + cancels + rejects)."""
        # 3 fills
        for i in range(3):
            analytics.record_order_sent(
                order_id=f"FILL-{i}", side="BUY", size=1,
                expected_price=21000.00,
            )
            analytics.record_fill(
                order_id=f"FILL-{i}", fill_price=21000.25, fill_size=1,
            )
        # 1 cancel
        analytics.record_order_sent(
            order_id="CANCEL-0", side="BUY", size=1,
            expected_price=21000.00,
        )
        analytics.record_cancel(order_id="CANCEL-0", reason="timeout")

        # 1 reject
        analytics.record_order_sent(
            order_id="REJECT-0", side="BUY", size=1,
            expected_price=21000.00,
        )
        analytics.record_rejection(order_id="REJECT-0", reason="halted")

        # 3 fills out of 5 total = 60%
        assert analytics.get_fill_rate() == 60.0

    def test_fill_rate_all_filled(self, analytics):
        """100% fill rate when all orders fill."""
        for i in range(5):
            analytics.record_order_sent(
                order_id=f"ALL-{i}", side="BUY", size=1,
                expected_price=21000.00,
            )
            analytics.record_fill(
                order_id=f"ALL-{i}", fill_price=21000.00, fill_size=1,
            )
        assert analytics.get_fill_rate() == 100.0

    def test_partial_fill_rate(self, analytics):
        """Partial fill rate counted correctly."""
        analytics.record_order_sent(
            order_id="PART-0", side="BUY", size=2,
            expected_price=21000.00,
        )
        analytics.record_fill(
            order_id="PART-0", fill_price=21000.00, fill_size=1,
        )
        # Size 2 but only 1 filled -> partial
        assert analytics.get_partial_fill_rate() == 100.0


# ══════════════════════════════════════════════════════════
# ROLLING AVERAGE TESTS
# ══════════════════════════════════════════════════════════

class TestRollingAverage:
    def test_rolling_average(self):
        """Rolling 20-trade window computes correct averages."""
        ea = ExecutionAnalytics(rolling_window=5)
        t0 = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)

        for i in range(10):
            oid = f"ROLL-{i}"
            ts = t0 + timedelta(minutes=i)
            ea.record_order_sent(
                order_id=oid, side="BUY", size=1,
                expected_price=21000.00, timestamp=ts,
            )
            # Each order has slippage = i * 0.25
            fill = 21000.00 + i * 0.25
            ea.record_fill(
                order_id=oid, fill_price=fill,
                fill_size=1, fill_timestamp=ts + timedelta(milliseconds=100),
            )

        metrics = ea.get_rolling_metrics()
        # Only last 5 trades in window: i=5,6,7,8,9
        # Slippage ticks: 5,6,7,8,9 -> avg = 7.0
        assert metrics["count"] == 5
        assert metrics["avg_slippage_ticks"] == 7.0
        assert metrics["avg_latency_ms"] == 100.0
        assert metrics["fill_rate"] == 100.0

    def test_rolling_empty(self, analytics):
        """Rolling metrics on empty analytics."""
        metrics = analytics.get_rolling_metrics()
        assert metrics["count"] == 0
        assert metrics["avg_slippage_ticks"] == 0.0


# ══════════════════════════════════════════════════════════
# TIME BUCKET TESTS
# ══════════════════════════════════════════════════════════

class TestTimeBucketAggregation:
    def test_time_bucket_aggregation(self):
        """Correct hourly buckets for ET times."""
        ea = ExecutionAnalytics()
        # 9:30 ET = 14:30 UTC, 10:00 ET = 15:00 UTC
        t_930 = datetime(2026, 3, 6, 14, 45, 0, tzinfo=timezone.utc)  # 9:45 ET
        t_1030 = datetime(2026, 3, 6, 15, 30, 0, tzinfo=timezone.utc)  # 10:30 ET

        ea.record_order_sent(
            order_id="TB-1", side="BUY", size=1,
            expected_price=21000.00, timestamp=t_930,
        )
        ea.record_fill(
            order_id="TB-1", fill_price=21000.25, fill_size=1,
            fill_timestamp=t_930 + timedelta(milliseconds=50),
        )

        ea.record_order_sent(
            order_id="TB-2", side="SELL", size=1,
            expected_price=21000.00, timestamp=t_1030,
        )
        ea.record_fill(
            order_id="TB-2", fill_price=20999.75, fill_size=1,
            fill_timestamp=t_1030 + timedelta(milliseconds=50),
        )

        buckets = ea.get_time_bucket_aggregates()
        assert buckets["09:30-10:00"]["total_orders"] == 1
        assert buckets["10:00-11:00"]["total_orders"] == 1


# ══════════════════════════════════════════════════════════
# REPORT HTML TESTS
# ══════════════════════════════════════════════════════════

class TestReportGeneration:
    def test_report_html_generation(self, populated_analytics):
        """Generated daily report contains valid HTML."""
        report = ExecutionReport(populated_analytics)
        html = report.generate_daily(date(2026, 3, 6))
        assert "<!DOCTYPE html>" in html
        assert "Execution Quality Report" in html
        assert "<table>" in html
        assert "</html>" in html

    def test_weekly_report_html(self, populated_analytics):
        """Generated weekly report is valid HTML."""
        report = ExecutionReport(populated_analytics)
        html = report.generate_weekly(date(2026, 3, 2))
        assert "<!DOCTYPE html>" in html
        assert "Weekly Execution Report" in html

    def test_scaling_readiness_report_html(self, populated_analytics):
        """Scaling readiness report is valid HTML."""
        report = ExecutionReport(populated_analytics)
        html = report.generate_scaling_readiness()
        assert "<!DOCTYPE html>" in html
        assert "Scaling Readiness" in html
        assert "Verdict" in html


# ══════════════════════════════════════════════════════════
# SCALING READINESS TESTS
# ══════════════════════════════════════════════════════════

class TestScalingReadiness:
    def test_scaling_readiness_verdict_ready(self):
        """Low-slippage, high fill rate -> ready to scale."""
        ea = ExecutionAnalytics()
        t0 = datetime.now(timezone.utc) - timedelta(days=5)

        for i in range(25):
            oid = f"SCALE-{i}"
            ts = t0 + timedelta(hours=i)
            ea.record_order_sent(
                order_id=oid, side="BUY", size=1,
                expected_price=21000.00, timestamp=ts,
            )
            # Low slippage: 0.25 pts = 1 tick
            ea.record_fill(
                order_id=oid, fill_price=21000.25, fill_size=1,
                fill_timestamp=ts + timedelta(milliseconds=80),
            )

        result = ea.assess_scaling_readiness(lookback_days=30)
        assert result["ready"] is True
        assert result["safe_contracts"] > 2

    def test_scaling_readiness_verdict_not_ready(self):
        """High slippage -> not ready to scale."""
        ea = ExecutionAnalytics()
        t0 = datetime.now(timezone.utc) - timedelta(days=5)

        for i in range(25):
            oid = f"HIGHSLIP-{i}"
            ts = t0 + timedelta(hours=i)
            ea.record_order_sent(
                order_id=oid, side="BUY", size=1,
                expected_price=21000.00, timestamp=ts,
            )
            # High slippage: 2.0 pts = 8 ticks
            ea.record_fill(
                order_id=oid, fill_price=21002.00, fill_size=1,
                fill_timestamp=ts + timedelta(milliseconds=80),
            )

        result = ea.assess_scaling_readiness(lookback_days=30)
        assert result["ready"] is False
        assert "slippage" in result["reason"].lower()

    def test_scaling_readiness_insufficient_data(self, analytics):
        """Too few trades -> not ready."""
        result = analytics.assess_scaling_readiness(lookback_days=30)
        assert result["ready"] is False
        assert "Insufficient data" in result["reason"]


# ══════════════════════════════════════════════════════════
# ANOMALY DETECTION TESTS
# ══════════════════════════════════════════════════════════

class TestAnomalyDetection:
    def test_anomaly_detection(self):
        """Flags fills > 3 standard deviations from mean slippage."""
        ea = ExecutionAnalytics()
        t0 = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)

        # 20 normal fills with slippage = 1 tick
        for i in range(20):
            oid = f"NORM-{i}"
            ts = t0 + timedelta(minutes=i)
            ea.record_order_sent(
                order_id=oid, side="BUY", size=1,
                expected_price=21000.00, timestamp=ts,
            )
            ea.record_fill(
                order_id=oid, fill_price=21000.25, fill_size=1,
                fill_timestamp=ts + timedelta(milliseconds=50),
            )

        # 1 outlier with slippage = 20 ticks (5 points)
        oid = "OUTLIER-0"
        ts = t0 + timedelta(minutes=30)
        ea.record_order_sent(
            order_id=oid, side="BUY", size=1,
            expected_price=21000.00, timestamp=ts,
        )
        ea.record_fill(
            order_id=oid, fill_price=21005.00, fill_size=1,
            fill_timestamp=ts + timedelta(milliseconds=50),
        )

        anomalies = ea.detect_anomalies(sigma_threshold=3.0)
        assert len(anomalies) >= 1
        anomaly_ids = [a.order_id for a in anomalies]
        assert "OUTLIER-0" in anomaly_ids

    def test_no_anomalies_when_consistent(self):
        """No anomalies when all fills have identical slippage."""
        ea = ExecutionAnalytics()
        t0 = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)
        for i in range(20):
            oid = f"CONS-{i}"
            ts = t0 + timedelta(minutes=i)
            ea.record_order_sent(
                order_id=oid, side="BUY", size=1,
                expected_price=21000.00, timestamp=ts,
            )
            ea.record_fill(
                order_id=oid, fill_price=21000.25, fill_size=1,
                fill_timestamp=ts + timedelta(milliseconds=50),
            )
        anomalies = ea.detect_anomalies(sigma_threshold=3.0)
        assert len(anomalies) == 0


# ══════════════════════════════════════════════════════════
# DATABASE STORAGE TESTS (MOCKED)
# ══════════════════════════════════════════════════════════

class TestDatabaseStorage:
    def test_database_storage(self):
        """Mock DB: verify insert is called with correct params."""
        async def _run():
            mock_db = AsyncMock()
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            mock_db.fetch = AsyncMock(return_value=[])

            ea = ExecutionAnalytics(db_manager=mock_db)

            t0 = datetime(2026, 3, 6, 14, 30, 0, tzinfo=timezone.utc)
            event = OrderEvent(
                order_id="DB-001",
                side="BUY",
                size=1,
                expected_price=21000.00,
                fill_price=21000.25,
                slippage_ticks=1.0,
                latency_ms=100,
                order_type="market",
                status="filled",
                order_sent_at=t0,
                fill_at=t0 + timedelta(milliseconds=100),
            )
            await ea._db_insert(event)

            mock_db.execute.assert_called_once()
            call_args = mock_db.execute.call_args
            assert "INSERT INTO execution_metrics" in call_args[0][0]
            assert call_args[0][1] == "DB-001"  # order_id
            assert call_args[0][2] == "BUY"     # side
            assert call_args[0][3] == 1         # size

        asyncio.run(_run())

    def test_load_from_db(self):
        """Mock DB: verify load populates analytics."""
        async def _run():
            mock_row = {
                "order_id": "LOADED-001",
                "side": "BUY",
                "size": 1,
                "expected_price": 21000.0,
                "fill_price": 21000.25,
                "slippage_ticks": 1.0,
                "latency_ms": 80,
                "order_type": "market",
                "status": "filled",
                "order_sent_at": datetime(2026, 3, 6, 14, 0, 0, tzinfo=timezone.utc),
                "fill_at": datetime(2026, 3, 6, 14, 0, 0, 80000, tzinfo=timezone.utc),
            }
            mock_db = AsyncMock()
            mock_db.fetch = AsyncMock(return_value=[mock_row])

            ea = ExecutionAnalytics(db_manager=mock_db)
            loaded = await ea.load_from_db(days=30)
            assert loaded == 1
            assert len(ea.get_all_events()) == 1

        asyncio.run(_run())


# ══════════════════════════════════════════════════════════
# CSV EXPORT TEST
# ══════════════════════════════════════════════════════════

class TestCSVExport:
    def test_export_csv(self, populated_analytics):
        """Export to CSV produces correct number of rows."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name

        try:
            count = populated_analytics.export_csv(path)
            assert count == 30

            with open(path, "r") as f:
                lines = f.readlines()
            # Header + 30 data rows
            assert len(lines) == 31
        finally:
            os.unlink(path)


# ══════════════════════════════════════════════════════════
# AGGREGATION TESTS
# ══════════════════════════════════════════════════════════

class TestAggregation:
    def test_daily_aggregate(self, populated_analytics):
        """Daily aggregate computes metrics for the correct day."""
        agg = populated_analytics.get_daily_aggregate(date(2026, 3, 6))
        assert agg["total_orders"] == 30
        assert agg["fill_rate_pct"] == 100.0
        assert agg["avg_slippage_ticks"] >= 0

    def test_direction_aggregates(self, populated_analytics):
        """Direction aggregates split BUY/SELL correctly."""
        dir_agg = populated_analytics.get_direction_aggregates()
        # Even indices are BUY (long_entry), odd are SELL (short_entry)
        assert dir_agg["long_entry"]["total_orders"] == 15
        assert dir_agg["short_entry"]["total_orders"] == 15

    def test_order_type_aggregates(self, populated_analytics):
        """Order type aggregates split market/limit correctly."""
        type_agg = populated_analytics.get_order_type_aggregates()
        # Every 3rd order is limit
        assert type_agg["limit"]["total_orders"] == 10
        assert type_agg["market"]["total_orders"] == 20
