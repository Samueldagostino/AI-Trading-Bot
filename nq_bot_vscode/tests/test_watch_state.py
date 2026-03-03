"""Tests for signals/watch_state.py — WatchStateManager."""

import pytest
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict
from signals.watch_state import WatchStateManager, WatchState, ConfirmedSignal
from signals.fvg_detector import FVGDetector, FairValueGap
from config.constants import (
    UCL_CONFIRMATION_BOOST,
    UCL_FVG_BOOST,
    UCL_FAST_CONFIRM_BOOST,
    UCL_HTF_ALIGN_BOOST,
)


@dataclass
class MockBar:
    """Minimal bar for testing."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 100


@dataclass
class MockHTFBias:
    """Minimal HTF bias mock."""
    htf_allows_long: bool = True
    htf_allows_short: bool = True
    consensus_direction: str = "neutral"
    consensus_strength: float = 0.0


def ts(minute=0):
    return datetime(2026, 3, 1, 10, minute, tzinfo=timezone.utc)


def make_bar(minute, o, h, l, c):
    return MockBar(timestamp=ts(minute), open=o, high=h, low=l, close=c)


def make_sweep_watch(direction="LONG", score=0.68, key_level=100.0,
                     invalidation=90.0, expiry=60, trigger_bar=0):
    """Helper: create a sweep-type watch state."""
    return WatchState(
        setup_type="sweep",
        direction=direction,
        trigger_bar=trigger_bar,
        trigger_price=100.0,
        key_level=key_level,
        invalidation_price=invalidation,
        expiry_bars=expiry,
        confirmation_conditions=["RECLAIM", "FVG_FORM", "FVG_TAP"],
        metadata={"sweep_low": 95.0, "sweep_depth": 5.0, "levels_swept": ["100.0"]},
        base_score=score,
        created_at=ts(0),
    )


def make_fvg_tap_watch(direction="LONG", score=0.65, fvg_high=105.0,
                       fvg_low=102.0, invalidation=100.0, expiry=45):
    """Helper: create an fvg_tap-type watch state."""
    return WatchState(
        setup_type="fvg_tap",
        direction=direction,
        trigger_bar=0,
        trigger_price=106.0,
        key_level=(fvg_high + fvg_low) / 2,
        invalidation_price=invalidation,
        expiry_bars=expiry,
        confirmation_conditions=["ENTER_ZONE", "HOLD", "CONTINUATION"],
        metadata={"fvg_high": fvg_high, "fvg_low": fvg_low, "is_inverse": False, "formation_bar": 0},
        base_score=score,
        created_at=ts(0),
    )


# ================================================================
# ADD WATCH TESTS
# ================================================================
class TestAddWatch:
    def test_add_basic(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch()
        result = mgr.add_watch(watch)
        assert result is True
        assert len(mgr.get_active_watches()) == 1

    def test_initializes_confirmations_met(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch()
        mgr.add_watch(watch)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met == {"RECLAIM": False, "FVG_FORM": False, "FVG_TAP": False}

    def test_max_active_watches(self):
        mgr = WatchStateManager()
        mgr.add_watch(make_sweep_watch(direction="LONG"))
        mgr.add_watch(make_sweep_watch(direction="SHORT"))
        mgr.add_watch(make_fvg_tap_watch(direction="LONG"))
        assert len(mgr.get_active_watches()) == 3

        # 4th watch — should evict oldest
        mgr.add_watch(make_fvg_tap_watch(direction="SHORT"))
        assert len(mgr.get_active_watches()) == 3

    def test_uniqueness_replaces_same_type_direction(self):
        mgr = WatchStateManager()
        watch1 = make_sweep_watch(direction="LONG", score=0.60)
        watch2 = make_sweep_watch(direction="LONG", score=0.70)
        mgr.add_watch(watch1)
        mgr.add_watch(watch2)
        watches = mgr.get_active_watches()
        assert len(watches) == 1
        assert watches[0].base_score == 0.70

    def test_different_type_not_replaced(self):
        mgr = WatchStateManager()
        mgr.add_watch(make_sweep_watch(direction="LONG"))
        mgr.add_watch(make_fvg_tap_watch(direction="LONG"))
        assert len(mgr.get_active_watches()) == 2

    def test_stats_created(self):
        mgr = WatchStateManager()
        mgr.add_watch(make_sweep_watch())
        stats = mgr.get_stats()
        assert stats["created"] == 1
        assert stats["active"] == 1


# ================================================================
# EXPIRY TESTS
# ================================================================
class TestExpiry:
    def test_watch_expires_after_n_bars(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch(expiry=5)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Process 5 bars — watch should expire on bar 5
        for i in range(5):
            bar = make_bar(i, 95, 96, 94, 95)  # below key_level, no reclaim
            mgr.update(bar, detector)

        assert len(mgr.get_active_watches()) == 0
        assert mgr.get_stats()["expired"] == 1

    def test_watch_still_active_before_expiry(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch(expiry=10)
        mgr.add_watch(watch)
        detector = FVGDetector()

        for i in range(9):
            bar = make_bar(i, 95, 96, 94, 95)
            mgr.update(bar, detector)

        assert len(mgr.get_active_watches()) == 1


# ================================================================
# INVALIDATION TESTS
# ================================================================
class TestInvalidation:
    def test_long_invalidated_close_below(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch(direction="LONG", invalidation=90.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        bar = make_bar(0, 91, 92, 88, 89)  # close=89 < invalidation=90
        mgr.update(bar, detector)
        assert len(mgr.get_active_watches()) == 0
        assert mgr.get_stats()["invalidated"] == 1

    def test_short_invalidated_close_above(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch(direction="SHORT", invalidation=110.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        bar = make_bar(0, 109, 112, 108, 111)  # close=111 > invalidation=110
        mgr.update(bar, detector)
        assert len(mgr.get_active_watches()) == 0

    def test_not_invalidated_at_boundary(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch(direction="LONG", invalidation=90.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        bar = make_bar(0, 91, 92, 89, 90)  # close=90 == invalidation — not below
        mgr.update(bar, detector)
        assert len(mgr.get_active_watches()) == 1


# ================================================================
# SWEEP CONFIRMATION TESTS
# ================================================================
class TestSweepConfirmation:
    def _setup_reclaim(self):
        """Create manager with sweep watch and reclaim it."""
        mgr = WatchStateManager()
        watch = make_sweep_watch(direction="LONG", key_level=100.0, invalidation=85.0)
        mgr.add_watch(watch)
        return mgr

    def test_reclaim_long(self):
        mgr = self._setup_reclaim()
        detector = FVGDetector()
        # Close above key_level=100
        bar = make_bar(0, 99, 102, 98, 101)
        mgr.update(bar, detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["RECLAIM"] is True

    def test_reclaim_not_met_below_level(self):
        mgr = self._setup_reclaim()
        detector = FVGDetector()
        bar = make_bar(0, 98, 99, 97, 99)  # close=99 < key_level=100
        mgr.update(bar, detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["RECLAIM"] is False

    def test_fvg_form_after_reclaim(self):
        mgr = self._setup_reclaim()
        # Need a real FVGDetector with FVGs
        detector = FVGDetector()

        # Bar 1: reclaim
        bar1 = make_bar(0, 99, 102, 98, 101)
        mgr.update(bar1, detector)

        # Create FVG in detector (3 bars that form a bullish gap)
        detector.update(make_bar(1, 101, 103, 100, 102), 1, "up")
        detector.update(make_bar(2, 103, 109, 102, 108), 2, "up")
        detector.update(make_bar(3, 108, 111, 106, 110), 3, "up")

        # Now update manager — should detect FVG_FORM
        bar4 = make_bar(4, 110, 112, 109, 111)
        mgr.update(bar4, detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["FVG_FORM"] is True

    def test_fvg_form_not_before_reclaim(self):
        """FVG_FORM should not be checked until RECLAIM is met."""
        mgr = WatchStateManager()
        watch = make_sweep_watch(direction="LONG", key_level=100.0, invalidation=85.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Create FVG in detector
        detector.update(make_bar(1, 101, 103, 100, 102), 1, "up")
        detector.update(make_bar(2, 103, 109, 102, 108), 2, "up")
        detector.update(make_bar(3, 108, 111, 106, 110), 3, "up")

        # Update manager with bar below key_level — no reclaim
        bar = make_bar(4, 98, 99, 97, 98)
        mgr.update(bar, detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["RECLAIM"] is False
        assert w.confirmations_met["FVG_FORM"] is False

    def test_full_sweep_confirmation_emits_signal(self):
        """Full sweep path: RECLAIM -> FVG_FORM -> FVG_TAP -> ConfirmedSignal."""
        mgr = WatchStateManager()
        watch = make_sweep_watch(
            direction="LONG", key_level=100.0, invalidation=85.0,
            score=0.68, trigger_bar=0,
        )
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Bar 1: reclaim (close above 100)
        confirmed = mgr.update(make_bar(1, 99, 102, 98, 101), detector)
        assert len(confirmed) == 0

        # Create bullish FVG in detector at 103-106
        detector.update(make_bar(2, 101, 103, 100, 102), 2, "up")
        detector.update(make_bar(3, 103, 109, 102, 108), 3, "up")
        detector.update(make_bar(4, 108, 111, 106, 110), 4, "up")

        # Bar 5: FVG_FORM check
        confirmed = mgr.update(make_bar(5, 110, 112, 109, 111), detector)
        assert len(confirmed) == 0

        # Bar 6: FVG_TAP — price returns to FVG zone (103-106), holds
        w = mgr.get_active_watches()[0]
        fvg_low = w.metadata.get("confirmed_fvg_low", 0)
        fvg_high = w.metadata.get("confirmed_fvg_high", 0)
        assert fvg_low > 0  # should be set
        # Bar enters and holds in the FVG zone
        confirmed = mgr.update(
            make_bar(6, 107, 107, fvg_low, fvg_low + 1), detector
        )
        assert len(confirmed) == 1

        cs = confirmed[0]
        assert cs.setup_type == "sweep"
        assert cs.direction == "LONG"
        assert cs.base_score == 0.68
        assert cs.boosted_score > cs.base_score

    def test_short_sweep_reclaim(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch(direction="SHORT", key_level=100.0, invalidation=115.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        bar = make_bar(0, 101, 102, 98, 99)  # close=99 < key_level=100
        mgr.update(bar, detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["RECLAIM"] is True


# ================================================================
# FVG TAP CONFIRMATION TESTS
# ================================================================
class TestFVGTapConfirmation:
    def test_enter_zone(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", fvg_high=105.0, fvg_low=102.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Bar enters zone (102-105)
        bar = make_bar(0, 106, 106, 103, 104)  # low=103, high=106 overlaps zone
        mgr.update(bar, detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["ENTER_ZONE"] is True

    def test_hold_after_enter(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", fvg_high=105.0, fvg_low=102.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Bar 1: enter zone
        mgr.update(make_bar(0, 106, 106, 103, 104), detector)
        # Bar 2: hold (close >= fvg_low=102)
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["HOLD"] is True

    def test_continuation_after_hold(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", fvg_high=105.0, fvg_low=102.0)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Enter zone
        mgr.update(make_bar(0, 106, 106, 103, 104), detector)
        # Hold
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)
        # Continuation: close > hold_close (103)
        confirmed = mgr.update(make_bar(2, 103, 106, 102, 105), detector)
        assert len(confirmed) == 1
        assert confirmed[0].direction == "LONG"

    def test_short_fvg_tap_full_cycle(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(
            direction="SHORT", fvg_high=108.0, fvg_low=105.0,
            invalidation=112.0, score=0.62,
        )
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Enter zone
        mgr.update(make_bar(0, 104, 107, 103, 106), detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["ENTER_ZONE"] is True

        # Hold (close <= fvg_high=108)
        mgr.update(make_bar(1, 106, 108, 105, 107), detector)
        w = mgr.get_active_watches()[0]
        assert w.confirmations_met["HOLD"] is True

        # Continuation (close < hold_close)
        hold_close = w.metadata["hold_close"]
        confirmed = mgr.update(make_bar(2, 107, 107, 104, hold_close - 1), detector)
        assert len(confirmed) == 1
        assert confirmed[0].direction == "SHORT"


# ================================================================
# SCORE BOOST TESTS
# ================================================================
class TestScoreBoost:
    def test_base_confirmation_boost(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", score=0.68)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Quick confirmation (3 bars < FAST_CONFIRM_THRESHOLD=20)
        mgr.update(make_bar(0, 106, 106, 103, 104), detector)  # enter
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)  # hold
        confirmed = mgr.update(make_bar(2, 103, 106, 102, 105), detector)  # continuation
        assert len(confirmed) == 1

        cs = confirmed[0]
        # FVG tap = fvg confluence = True
        # 3 bars < 20 = fast confirm
        expected = 0.68 + UCL_CONFIRMATION_BOOST + UCL_FVG_BOOST + UCL_FAST_CONFIRM_BOOST
        assert cs.boosted_score == round(expected, 3)

    def test_htf_alignment_boost(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", score=0.68)
        mgr.add_watch(watch)
        detector = FVGDetector()
        htf_bias = MockHTFBias(htf_allows_long=True)

        mgr.update(make_bar(0, 106, 106, 103, 104), detector)
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)
        confirmed = mgr.update(make_bar(2, 103, 106, 102, 105), detector, htf_bias=htf_bias)
        assert len(confirmed) == 1

        cs = confirmed[0]
        assert cs.htf_aligned is True
        expected = 0.68 + UCL_CONFIRMATION_BOOST + UCL_FVG_BOOST + UCL_FAST_CONFIRM_BOOST + UCL_HTF_ALIGN_BOOST
        assert cs.boosted_score == round(expected, 3)

    def test_no_htf_boost_when_not_aligned(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", score=0.68)
        mgr.add_watch(watch)
        detector = FVGDetector()
        htf_bias = MockHTFBias(htf_allows_long=False)

        mgr.update(make_bar(0, 106, 106, 103, 104), detector)
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)
        confirmed = mgr.update(make_bar(2, 103, 106, 102, 105), detector, htf_bias=htf_bias)
        cs = confirmed[0]
        assert cs.htf_aligned is False
        expected = 0.68 + UCL_CONFIRMATION_BOOST + UCL_FVG_BOOST + UCL_FAST_CONFIRM_BOOST
        assert cs.boosted_score == round(expected, 3)

    def test_slow_confirm_no_fast_boost(self):
        """No fast-confirm boost if confirmation takes >= 20 bars."""
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", score=0.68, expiry=50)
        mgr.add_watch(watch)
        detector = FVGDetector()

        # Burn 19 bars without progress
        for i in range(19):
            mgr.update(make_bar(i, 110, 112, 109, 111), detector)

        # Now confirm on bars 19, 20, 21 (bars_elapsed >= 20)
        mgr.update(make_bar(19, 106, 106, 103, 104), detector)  # enter
        mgr.update(make_bar(20, 104, 105, 102, 103), detector)  # hold
        confirmed = mgr.update(make_bar(21, 103, 106, 102, 105), detector)  # continuation

        assert len(confirmed) == 1
        cs = confirmed[0]
        assert cs.bars_to_confirm >= 20
        # Should NOT include fast-confirm boost
        expected = 0.68 + UCL_CONFIRMATION_BOOST + UCL_FVG_BOOST
        assert cs.boosted_score == round(expected, 3)

    def test_score_capped_at_1(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", score=0.92)
        mgr.add_watch(watch)
        detector = FVGDetector()

        mgr.update(make_bar(0, 106, 106, 103, 104), detector)
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)
        confirmed = mgr.update(make_bar(2, 103, 106, 102, 105), detector)
        assert confirmed[0].boosted_score <= 1.0


# ================================================================
# CANCEL TESTS
# ================================================================
class TestCancel:
    def test_cancel_by_id(self):
        mgr = WatchStateManager()
        watch = make_sweep_watch()
        mgr.add_watch(watch)
        assert len(mgr.get_active_watches()) == 1

        mgr.cancel(watch.watch_id)
        assert len(mgr.get_active_watches()) == 0

    def test_cancel_nonexistent_id(self):
        mgr = WatchStateManager()
        mgr.cancel("nonexistent")  # should not raise


# ================================================================
# CONFIRMED SIGNAL FIELDS
# ================================================================
class TestConfirmedSignalFields:
    def test_all_fields_populated(self):
        mgr = WatchStateManager()
        watch = make_fvg_tap_watch(direction="LONG", score=0.70)
        mgr.add_watch(watch)
        detector = FVGDetector()

        mgr.update(make_bar(0, 106, 106, 103, 104), detector)
        mgr.update(make_bar(1, 104, 105, 102, 103), detector)
        confirmed = mgr.update(make_bar(2, 103, 106, 102, 105), detector)
        cs = confirmed[0]

        assert cs.watch_id == watch.watch_id
        assert cs.setup_type == "fvg_tap"
        assert cs.direction == "LONG"
        assert cs.base_score == 0.70
        assert cs.boosted_score > 0.70
        assert cs.bars_to_confirm == 3
        assert cs.confirmation_price == 105.0
        assert cs.has_fvg_confluence is True


# ================================================================
# WATCH STATE DATACLASS TESTS
# ================================================================
class TestWatchStateDataclass:
    def test_is_confirmed_false(self):
        w = make_sweep_watch()
        w.confirmations_met = {"RECLAIM": True, "FVG_FORM": False, "FVG_TAP": False}
        assert w.is_confirmed is False

    def test_is_confirmed_true(self):
        w = make_sweep_watch()
        w.confirmations_met = {"RECLAIM": True, "FVG_FORM": True, "FVG_TAP": True}
        assert w.is_confirmed is True

    def test_is_confirmed_empty(self):
        w = WatchState()
        assert w.is_confirmed is False


# ================================================================
# STATS TESTS
# ================================================================
class TestStats:
    def test_initial_stats(self):
        mgr = WatchStateManager()
        stats = mgr.get_stats()
        assert stats["created"] == 0
        assert stats["confirmed"] == 0
        assert stats["expired"] == 0
        assert stats["invalidated"] == 0
        assert stats["active"] == 0

    def test_stats_after_operations(self):
        mgr = WatchStateManager()
        detector = FVGDetector()

        # Add 2 watches
        mgr.add_watch(make_sweep_watch(direction="LONG", expiry=2))
        # SHORT watch: invalidation at 110 so close=96 won't trigger it
        mgr.add_watch(make_sweep_watch(direction="SHORT", invalidation=110.0, expiry=100))

        # Expire the LONG watch (2 bars), SHORT watch stays active
        mgr.update(make_bar(0, 96, 97, 95, 96), detector)
        mgr.update(make_bar(1, 96, 97, 95, 96), detector)

        stats = mgr.get_stats()
        assert stats["created"] == 2
        assert stats["expired"] == 1
        assert stats["active"] == 1


# ================================================================
# CONSTANTS TESTS
# ================================================================
class TestConstants:
    def test_max_active_watches(self):
        assert WatchStateManager.MAX_ACTIVE_WATCHES == 3

    def test_fast_confirm_threshold(self):
        assert WatchStateManager.FAST_CONFIRM_THRESHOLD == 20

    def test_ucl_constants(self):
        assert UCL_CONFIRMATION_BOOST == 0.10
        assert UCL_FVG_BOOST == 0.05
        assert UCL_FAST_CONFIRM_BOOST == 0.05
        assert UCL_HTF_ALIGN_BOOST == 0.05
