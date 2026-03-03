"""Tests for signals/fvg_detector.py — FVGDetector."""

import pytest
from datetime import datetime, timezone
from dataclasses import dataclass
from signals.fvg_detector import FVGDetector, FairValueGap


@dataclass
class MockBar:
    """Minimal bar for testing."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 100


def ts(minute=0):
    """Shorthand for deterministic timestamps."""
    return datetime(2026, 3, 1, 10, minute, tzinfo=timezone.utc)


def make_bar(minute, o, h, l, c):
    return MockBar(timestamp=ts(minute), open=o, high=h, low=l, close=c)


# ================================================================
# DETECTION TESTS
# ================================================================
class TestBullishFVGDetection:
    """Bullish FVG: candle[0].high < candle[2].low (gap up)."""

    def test_basic_bullish_fvg(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),   # candle[0]: high=102
            make_bar(1, 102, 108, 101, 107),   # candle[1]: impulse
            make_bar(2, 107, 110, 105, 109),   # candle[2]: low=105
        ]
        # gap = candle[2].low(105) - candle[0].high(102) = 3.0 pts >= MIN_FVG_SIZE
        new_fvgs = []
        for i, bar in enumerate(bars):
            result = detector.update(bar, i, "up")
            new_fvgs.extend(result)

        assert len(new_fvgs) == 1
        fvg = new_fvgs[0]
        assert fvg.direction == "bullish"
        assert fvg.fvg_low == 102.0   # candle[0].high
        assert fvg.fvg_high == 105.0  # candle[2].low
        assert fvg.size_points == 3.0
        assert fvg.status == "UNFILLED"

    def test_bullish_fvg_midpoint(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 106, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")
        fvgs = detector.get_active_fvgs("bullish")
        assert len(fvgs) == 1
        assert fvgs[0].fvg_midpoint == round((106 + 102) / 2, 2)

    def test_too_small_gap_rejected(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),   # high=102
            make_bar(1, 102, 104, 101, 103),   # impulse
            make_bar(2, 103, 105, 103, 104),   # low=103, gap=103-102=1.0 < MIN
        ]
        new_fvgs = []
        for i, bar in enumerate(bars):
            new_fvgs.extend(detector.update(bar, i, "up"))
        assert len(new_fvgs) == 0

    def test_inverse_bullish_fvg(self):
        """Bullish FVG forming in a downtrend is inverse."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "down")  # downtrend
        fvgs = detector.get_active_fvgs("bullish")
        assert len(fvgs) == 1
        assert fvgs[0].is_inverse is True

    def test_non_inverse_bullish_fvg(self):
        """Bullish FVG forming in an uptrend is not inverse."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")
        fvgs = detector.get_active_fvgs("bullish")
        assert fvgs[0].is_inverse is False


class TestBearishFVGDetection:
    """Bearish FVG: candle[0].low > candle[2].high (gap down)."""

    def test_basic_bearish_fvg(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 110, 112, 108, 109),  # candle[0]: low=108
            make_bar(1, 109, 109, 102, 103),   # candle[1]: impulse down
            make_bar(2, 103, 105, 101, 104),   # candle[2]: high=105
        ]
        # gap = candle[0].low(108) - candle[2].high(105) = 3.0 pts
        new_fvgs = []
        for i, bar in enumerate(bars):
            new_fvgs.extend(detector.update(bar, i, "down"))
        assert len(new_fvgs) == 1
        fvg = new_fvgs[0]
        assert fvg.direction == "bearish"
        assert fvg.fvg_high == 108.0  # candle[0].low
        assert fvg.fvg_low == 105.0   # candle[2].high
        assert fvg.size_points == 3.0

    def test_inverse_bearish_fvg(self):
        """Bearish FVG forming in an uptrend is inverse."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 110, 112, 108, 109),
            make_bar(1, 109, 109, 102, 103),
            make_bar(2, 103, 105, 101, 104),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")  # uptrend
        fvgs = detector.get_active_fvgs("bearish")
        assert len(fvgs) == 1
        assert fvgs[0].is_inverse is True


class TestNoFVGDetected:
    """Cases where no FVG should be detected."""

    def test_overlapping_candles(self):
        """No gap when candles overlap."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 105, 98, 103),
            make_bar(1, 103, 107, 101, 106),
            make_bar(2, 106, 108, 103, 107),  # low=103 < candle[0].high=105
        ]
        new_fvgs = []
        for i, bar in enumerate(bars):
            new_fvgs.extend(detector.update(bar, i, "up"))
        assert len(new_fvgs) == 0

    def test_insufficient_bars(self):
        detector = FVGDetector()
        bar = make_bar(0, 100, 105, 98, 103)
        result = detector.update(bar, 0, "up")
        assert result == []

    def test_two_bars_not_enough(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
        ]
        new_fvgs = []
        for i, bar in enumerate(bars):
            new_fvgs.extend(detector.update(bar, i, "up"))
        assert len(new_fvgs) == 0


class TestDuplicateDetection:
    """Deduplication of FVGs at similar levels."""

    def test_no_duplicates(self):
        detector = FVGDetector()
        # Create same FVG pattern twice
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")

        # Add another bar that doesn't create a new pattern, then repeat similar
        detector.update(make_bar(3, 109, 112, 108, 111), 3, "up")
        # Now re-check — should still have only 1
        assert len(detector.get_active_fvgs("bullish")) == 1


# ================================================================
# LIFECYCLE TESTS
# ================================================================
class TestFVGLifecycle:
    """Test UNFILLED -> PARTIALLY_FILLED -> FILLED -> VIOLATED transitions."""

    def _create_bullish_fvg(self):
        """Helper: create a detector with one bullish FVG at 102-105."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")
        return detector

    def test_starts_unfilled(self):
        detector = self._create_bullish_fvg()
        fvgs = detector.get_active_fvgs("bullish")
        assert fvgs[0].status == "UNFILLED"

    def test_partially_filled_on_zone_entry(self):
        detector = self._create_bullish_fvg()
        # Price enters zone (102-105) but doesn't reach midpoint
        bar = make_bar(3, 106, 106, 104, 105)  # low=104 enters zone
        detector.update(bar, 3, "up")
        fvgs = detector.get_active_fvgs("bullish")
        assert fvgs[0].status == "PARTIALLY_FILLED"

    def test_filled_on_midpoint_touch(self):
        detector = self._create_bullish_fvg()
        # Midpoint = (102 + 105) / 2 = 103.5
        # First enter zone
        bar1 = make_bar(3, 106, 106, 104, 105)
        detector.update(bar1, 3, "up")
        # Then touch midpoint
        bar2 = make_bar(4, 105, 105, 103, 104)  # low=103 <= midpoint=103.5
        detector.update(bar2, 4, "up")
        fvgs = detector.get_active_fvgs("bullish")
        assert fvgs[0].status == "FILLED"

    def test_violated_bullish_close_below(self):
        """Bullish FVG violated when bar closes below fvg_low."""
        detector = self._create_bullish_fvg()
        # Close below fvg_low (102)
        bar = make_bar(3, 103, 103, 100, 101)  # close=101 < fvg_low=102
        detector.update(bar, 3, "up")
        fvgs = detector.get_active_fvgs("bullish")
        assert len(fvgs) == 0  # violated FVGs are removed

    def test_violated_bearish_close_above(self):
        """Bearish FVG violated when bar closes above fvg_high."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 110, 112, 108, 109),
            make_bar(1, 109, 109, 102, 103),
            make_bar(2, 103, 105, 101, 104),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "down")

        fvgs = detector.get_active_fvgs("bearish")
        assert len(fvgs) == 1
        assert fvgs[0].fvg_high == 108.0

        # Close above fvg_high (108)
        bar = make_bar(3, 107, 110, 106, 109)  # close=109 > fvg_high=108
        detector.update(bar, 3, "down")
        fvgs = detector.get_active_fvgs("bearish")
        assert len(fvgs) == 0

    def test_expiry(self):
        """FVGs expire after EXPIRY_BARS."""
        detector = self._create_bullish_fvg()
        assert len(detector.get_active_fvgs("bullish")) == 1

        # Fast-forward past expiry
        bar = make_bar(4, 106, 107, 105, 106)
        detector.update(bar, FVGDetector.EXPIRY_BARS + 5, "up")
        assert len(detector.get_active_fvgs("bullish")) == 0


# ================================================================
# ZONE INTERACTION TESTS
# ================================================================
class TestZoneInteraction:
    """Test check_zone_interaction()."""

    def _create_bullish_fvg(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")
        return detector

    def test_hold_interaction(self):
        """Bar enters zone and closes within it."""
        detector = self._create_bullish_fvg()
        bar = make_bar(3, 106, 106, 103, 104)  # enters zone, closes within
        interactions = detector.check_zone_interaction(bar)
        assert len(interactions) == 1
        fvg, itype = interactions[0]
        assert itype == "HOLD"

    def test_violate_interaction(self):
        """Bar enters zone and closes through it."""
        detector = self._create_bullish_fvg()
        bar = make_bar(3, 103, 103, 100, 101)  # closes below fvg_low=102
        interactions = detector.check_zone_interaction(bar)
        assert len(interactions) == 1
        _, itype = interactions[0]
        assert itype == "VIOLATE"

    def test_no_interaction_outside_zone(self):
        """Bar completely outside zone."""
        detector = self._create_bullish_fvg()
        bar = make_bar(3, 110, 115, 109, 113)  # entirely above zone
        interactions = detector.check_zone_interaction(bar)
        assert len(interactions) == 0


# ================================================================
# MAX ACTIVE / STATS TESTS
# ================================================================
class TestMaxActiveAndStats:
    """Test eviction and stats."""

    def test_max_active_per_direction(self):
        detector = FVGDetector()
        detector.MAX_ACTIVE_PER_DIRECTION = 3  # lower for test

        # Create 4 bullish FVGs at different levels
        for n in range(4):
            base = 100 + n * 20
            bars = [
                make_bar(n * 3, base, base + 2, base - 1, base + 1),
                make_bar(n * 3 + 1, base + 2, base + 8, base + 1, base + 7),
                make_bar(n * 3 + 2, base + 7, base + 10, base + 5, base + 9),
            ]
            for i, bar in enumerate(bars):
                detector.update(bar, n * 3 + i, "up")

        active = detector.get_active_fvgs("bullish")
        assert len(active) <= 3

    def test_stats(self):
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")

        stats = detector.get_stats()
        assert stats["total_detected"] == 1
        assert stats["active_bullish"] == 1
        assert stats["active_bearish"] == 0
        assert stats["total_active"] == 1

    def test_get_active_fvgs_all(self):
        """get_active_fvgs() with no direction returns all."""
        detector = FVGDetector()
        # Bullish FVG: gap up at 102-105
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(1, 102, 108, 101, 107),
            make_bar(2, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")

        # Transition bars to bridge price levels smoothly
        detector.update(make_bar(3, 109, 112, 108, 111), 3, "none")
        detector.update(make_bar(4, 111, 114, 110, 113), 4, "none")
        detector.update(make_bar(5, 113, 116, 112, 115), 5, "none")

        # Bearish FVG: gap down at 112-109
        detector.update(make_bar(6, 115, 116, 112, 113), 6, "down")
        detector.update(make_bar(7, 113, 113, 106, 107), 7, "down")
        detector.update(make_bar(8, 107, 109, 105, 108), 8, "down")

        all_fvgs = detector.get_active_fvgs()
        bullish = [f for f in all_fvgs if f.direction == "bullish"]
        bearish = [f for f in all_fvgs if f.direction == "bearish"]
        assert len(bullish) >= 1
        assert len(bearish) >= 1
        assert len(all_fvgs) >= 2

    def test_formation_time(self):
        """formation_time is the middle candle's timestamp."""
        detector = FVGDetector()
        bars = [
            make_bar(0, 100, 102, 99, 101),
            make_bar(5, 102, 108, 101, 107),  # minute 5
            make_bar(10, 107, 110, 105, 109),
        ]
        for i, bar in enumerate(bars):
            detector.update(bar, i, "up")
        fvgs = detector.get_active_fvgs("bullish")
        assert fvgs[0].formation_time == ts(5)


# ================================================================
# CONSTANTS TESTS
# ================================================================
class TestConstants:
    def test_min_fvg_size(self):
        assert FVGDetector.MIN_FVG_SIZE == 2.0

    def test_max_active(self):
        assert FVGDetector.MAX_ACTIVE_PER_DIRECTION == 20

    def test_expiry(self):
        assert FVGDetector.EXPIRY_BARS == 500
