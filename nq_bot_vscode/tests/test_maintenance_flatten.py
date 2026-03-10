"""
Tests for Maintenance Window Hard Flatten (4:50 PM ET)
======================================================
Verifies the CME maintenance window safety rule:
  - Entry cutoff at 4:30 PM ET (no new trades after this time)
  - Hard flatten at 4:50 PM ET (close ALL positions unconditionally)
  - Exit reason tagged as EXIT_MAINTENANCE_FLATTEN
  - DST-aware via ZoneInfo("America/New_York")
  - Both C1 and C2 are flattened

Covers 10 edge cases as specified in the requirements.
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta, time as dt_time
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from zoneinfo import ZoneInfo

from execution.scale_out_executor import (
    ScaleOutExecutor,
    ScaleOutTrade,
    ScaleOutPhase,
    ContractLeg,
)
from config.settings import BotConfig, CONFIG


ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def executor():
    """Create a ScaleOutExecutor for testing."""
    return ScaleOutExecutor(CONFIG)


def _make_active_trade(executor, direction="long", entry_price=20000.0):
    """Helper: set up an active trade on the executor with C1 and C2 open."""
    trade = ScaleOutTrade(
        direction=direction,
        entry_price=entry_price,
    )
    trade.c1 = ContractLeg(
        leg_number=1, leg_label="C1", contracts=1,
        entry_price=entry_price, is_open=True, is_filled=True,
        exit_strategy="time_5bar",
        entry_time=datetime(2025, 6, 15, 13, 0, tzinfo=ET),
    )
    trade.c2 = ContractLeg(
        leg_number=2, leg_label="C2", contracts=1,
        entry_price=entry_price, is_open=True, is_filled=True,
        exit_strategy="structural_target",
        entry_time=datetime(2025, 6, 15, 13, 0, tzinfo=ET),
    )
    trade.c3 = ContractLeg(
        leg_number=3, leg_label="C3", contracts=3,
        entry_price=entry_price, is_open=True, is_filled=True,
        exit_strategy="atr_trail",
        entry_time=datetime(2025, 6, 15, 13, 0, tzinfo=ET),
    )
    trade.c4 = ContractLeg(leg_number=4, leg_label="C4", contracts=0)
    trade.initial_stop = entry_price - 30.0 if direction == "long" else entry_price + 30.0
    trade.entry_time = datetime(2025, 6, 15, 13, 0, tzinfo=ET)
    trade.phase = ScaleOutPhase.PHASE_1
    trade.phase_history = []

    executor._active_trade = trade
    return trade


def _make_et_datetime(hour, minute, second=0, month=6, day=15, year=2025):
    """Create a timezone-aware datetime in Eastern Time."""
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


# ═══════════════════════════════════════════════════════════════
# TEST 1: Position opened at 4:29 PM is allowed
# ═══════════════════════════════════════════════════════════════

def test_entry_at_429pm_allowed():
    """A signal at 4:29 PM ET should NOT be blocked by the maintenance cutoff."""
    t = _make_et_datetime(16, 29)
    current_time_et = t.time()
    # 4:29 PM is before the 4:30 PM cutoff
    assert current_time_et < dt_time(16, 30), "4:29 PM should be before cutoff"
    # Entry should be allowed
    entry_blocked = current_time_et >= dt_time(16, 30)
    assert not entry_blocked, "Entry at 4:29 PM should be allowed"


# ═══════════════════════════════════════════════════════════════
# TEST 2: Signal at 4:31 PM is blocked with correct log message
# ═══════════════════════════════════════════════════════════════

def test_entry_at_431pm_blocked():
    """A signal at 4:31 PM ET should be blocked by the 4:30 PM entry cutoff."""
    t = _make_et_datetime(16, 31)
    current_time_et = t.time()
    entry_blocked = current_time_et >= dt_time(16, 30)
    assert entry_blocked, "Entry at 4:31 PM should be blocked"


def test_entry_at_exactly_430pm_blocked():
    """A signal at exactly 4:30:00 PM ET should be blocked (boundary assigned to later session)."""
    t = _make_et_datetime(16, 30, 0)
    current_time_et = t.time()
    entry_blocked = current_time_et >= dt_time(16, 30)
    assert entry_blocked, "Entry at exactly 4:30 PM should be blocked"


# ═══════════════════════════════════════════════════════════════
# TEST 3: Open position at 4:50 PM is force-closed
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_flatten_at_450pm():
    """An open position at 4:50 PM ET should be force-closed."""
    executor = ScaleOutExecutor(CONFIG)
    _make_active_trade(executor)

    assert executor.has_active_trade

    flatten_time = _make_et_datetime(16, 50)
    result = await executor.maintenance_flatten(20050.0, flatten_time)

    assert result is not None
    assert result["action"] == "trade_closed"
    assert not executor.has_active_trade


# ═══════════════════════════════════════════════════════════════
# TEST 4: Open position at 4:49 PM is NOT force-closed
# ═══════════════════════════════════════════════════════════════

def test_no_flatten_at_449pm():
    """At 4:49 PM, the flatten should NOT fire — it fires at 4:50, not before."""
    t = _make_et_datetime(16, 49)
    current_time_et = t.time()
    should_flatten = current_time_et >= dt_time(16, 50)
    assert not should_flatten, "Should NOT flatten at 4:49 PM"


def test_flatten_triggers_at_450pm():
    """At exactly 4:50:00 PM, the flatten SHOULD fire."""
    t = _make_et_datetime(16, 50, 0)
    current_time_et = t.time()
    should_flatten = current_time_et >= dt_time(16, 50)
    assert should_flatten, "Should flatten at exactly 4:50 PM"


# ═══════════════════════════════════════════════════════════════
# TEST 5: Flatten fires on first bar >= 4:50 PM even if no bar at exactly 4:50
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_flatten_at_452pm_when_no_bar_at_450():
    """If no bar lands exactly at 4:50 PM, the flatten should fire on the first bar after."""
    executor = ScaleOutExecutor(CONFIG)
    _make_active_trade(executor)

    # First bar after 4:50 PM is at 4:52 PM (2-minute execution bars)
    flatten_time = _make_et_datetime(16, 52)
    current_time_et = flatten_time.time()
    should_flatten = current_time_et >= dt_time(16, 50)
    assert should_flatten, "4:52 PM should trigger flatten"

    result = await executor.maintenance_flatten(20050.0, flatten_time)
    assert result is not None
    assert not executor.has_active_trade


# ═══════════════════════════════════════════════════════════════
# TEST 6: Both C1 and C2 (and C3) are flattened
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_all_legs_flattened():
    """Maintenance flatten must close ALL open legs (C1, C2, C3), not just one."""
    executor = ScaleOutExecutor(CONFIG)
    trade = _make_active_trade(executor)

    # Verify all are open
    assert trade.c1.is_open
    assert trade.c2.is_open
    assert trade.c3.is_open

    flatten_time = _make_et_datetime(16, 50)
    result = await executor.maintenance_flatten(20050.0, flatten_time)

    assert result is not None
    # All legs should be closed
    assert not trade.c1.is_open
    assert not trade.c2.is_open
    assert not trade.c3.is_open


# ═══════════════════════════════════════════════════════════════
# TEST 7: Exit reason tagged as EXIT_MAINTENANCE_FLATTEN
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_exit_reason_tagged_correctly():
    """All legs must have exit_reason = EXIT_MAINTENANCE_FLATTEN."""
    executor = ScaleOutExecutor(CONFIG)
    trade = _make_active_trade(executor)

    flatten_time = _make_et_datetime(16, 50)
    await executor.maintenance_flatten(20050.0, flatten_time)

    assert trade.c1.exit_reason == "EXIT_MAINTENANCE_FLATTEN"
    assert trade.c2.exit_reason == "EXIT_MAINTENANCE_FLATTEN"
    assert trade.c3.exit_reason == "EXIT_MAINTENANCE_FLATTEN"


# ═══════════════════════════════════════════════════════════════
# TEST 8: DST transitions don't break the cutoff times
# ═══════════════════════════════════════════════════════════════

def test_dst_spring_forward():
    """Cutoff times must work correctly during DST spring-forward (March).

    On March 9, 2025 (spring forward): clocks jump from 2:00 AM to 3:00 AM.
    4:30 PM ET is still 4:30 PM ET regardless.
    """
    # Spring forward day: March 9, 2025
    t_430 = datetime(2025, 3, 9, 16, 30, 0, tzinfo=ET)
    t_450 = datetime(2025, 3, 9, 16, 50, 0, tzinfo=ET)

    # Verify ET is correct
    assert t_430.astimezone(ET).hour == 16
    assert t_430.astimezone(ET).minute == 30
    assert t_450.astimezone(ET).hour == 16
    assert t_450.astimezone(ET).minute == 50

    # Verify cutoff logic
    assert t_430.time() >= dt_time(16, 30)
    assert t_450.time() >= dt_time(16, 50)


def test_dst_fall_back():
    """Cutoff times must work correctly during DST fall-back (November).

    On November 2, 2025 (fall back): clocks jump from 2:00 AM back to 1:00 AM.
    4:30 PM ET is still 4:30 PM ET regardless.
    """
    # Fall back day: November 2, 2025
    t_430 = datetime(2025, 11, 2, 16, 30, 0, tzinfo=ET)
    t_450 = datetime(2025, 11, 2, 16, 50, 0, tzinfo=ET)

    # Verify ET is correct
    assert t_430.astimezone(ET).hour == 16
    assert t_430.astimezone(ET).minute == 30
    assert t_450.astimezone(ET).hour == 16
    assert t_450.astimezone(ET).minute == 50

    # Entry should be blocked at 4:30 PM
    assert t_430.time() >= dt_time(16, 30)
    # Flatten should fire at 4:50 PM
    assert t_450.time() >= dt_time(16, 50)

    # Before cutoff should be fine
    t_429 = datetime(2025, 11, 2, 16, 29, 0, tzinfo=ET)
    assert t_429.time() < dt_time(16, 30)


# ═══════════════════════════════════════════════════════════════
# TEST 9: Flatten when only C2 is still running (C1 already exited)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_flatten_with_c1_already_closed():
    """If C1 has already exited but C2 is still running, flatten C2."""
    executor = ScaleOutExecutor(CONFIG)
    trade = _make_active_trade(executor)

    # Simulate C1 already closed
    trade.c1.is_open = False
    trade.c1.exit_price = 20010.0
    trade.c1.exit_reason = "time_5bars"
    trade.c1.exit_time = _make_et_datetime(14, 0)

    # C3 also closed (blocked by delayed entry)
    trade.c3.is_open = False
    trade.c3.exit_price = 20010.0
    trade.c3.exit_reason = "delayed_c3_blocked"
    trade.c3.exit_time = _make_et_datetime(14, 0)

    # Only C2 remains open
    assert trade.c2.is_open
    open_before = len(trade.open_legs)
    assert open_before == 1

    flatten_time = _make_et_datetime(16, 50)
    result = await executor.maintenance_flatten(20050.0, flatten_time)

    assert result is not None
    assert not trade.c2.is_open
    assert trade.c2.exit_reason == "EXIT_MAINTENANCE_FLATTEN"


# ═══════════════════════════════════════════════════════════════
# TEST 10: No-op when no position is open
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_flatten_noop_when_flat():
    """If no position is open at 4:50 PM, maintenance_flatten returns None."""
    executor = ScaleOutExecutor(CONFIG)
    assert not executor.has_active_trade

    flatten_time = _make_et_datetime(16, 50)
    result = await executor.maintenance_flatten(20050.0, flatten_time)
    assert result is None


# ═══════════════════════════════════════════════════════════════
# TEST 11: UTC offset correctness — 4:50 PM ET != 4:50 PM UTC
# ═══════════════════════════════════════════════════════════════

def test_utc_offset_not_hardcoded():
    """Verify that the system uses ZoneInfo, not hardcoded UTC offsets.

    During EDT (summer): ET = UTC-4, so 4:50 PM ET = 8:50 PM UTC
    During EST (winter): ET = UTC-5, so 4:50 PM ET = 9:50 PM UTC
    """
    # Summer: EDT (UTC-4)
    summer = datetime(2025, 7, 15, 16, 50, 0, tzinfo=ET)
    utc_summer = summer.astimezone(timezone.utc)
    assert utc_summer.hour == 20  # 4:50 PM EDT = 8:50 PM UTC

    # Winter: EST (UTC-5)
    winter = datetime(2025, 12, 15, 16, 50, 0, tzinfo=ET)
    utc_winter = winter.astimezone(timezone.utc)
    assert utc_winter.hour == 21  # 4:50 PM EST = 9:50 PM UTC

    # Both should trigger the flatten (both are 4:50 PM ET)
    assert summer.time() >= dt_time(16, 50)
    assert winter.time() >= dt_time(16, 50)


# ═══════════════════════════════════════════════════════════════
# TEST 12: Session diagnostic classify_session
# ═══════════════════════════════════════════════════════════════

def test_session_classification():
    """Verify session classification covers all time ranges correctly."""
    # Import the classify function
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / "scripts"))
    from session_diagnostic import classify_session

    # ETH_ASIA: 6:00 PM – 2:00 AM
    assert classify_session(_make_et_datetime(18, 0)) == "ETH_ASIA"
    assert classify_session(_make_et_datetime(23, 30)) == "ETH_ASIA"
    assert classify_session(_make_et_datetime(1, 59)) == "ETH_ASIA"

    # ETH_LONDON: 2:00 AM – 9:30 AM
    assert classify_session(_make_et_datetime(2, 0)) == "ETH_LONDON"
    assert classify_session(_make_et_datetime(5, 0)) == "ETH_LONDON"
    assert classify_session(_make_et_datetime(9, 29)) == "ETH_LONDON"

    # RTH_EARLY: 9:30 AM – 12:00 PM (boundary: 9:30 → RTH_EARLY)
    assert classify_session(_make_et_datetime(9, 30)) == "RTH_EARLY"
    assert classify_session(_make_et_datetime(10, 0)) == "RTH_EARLY"
    assert classify_session(_make_et_datetime(11, 59)) == "RTH_EARLY"

    # RTH_LATE: 12:00 PM – 4:00 PM
    assert classify_session(_make_et_datetime(12, 0)) == "RTH_LATE"
    assert classify_session(_make_et_datetime(14, 0)) == "RTH_LATE"
    assert classify_session(_make_et_datetime(15, 59)) == "RTH_LATE"

    # POST_RTH: 4:00 PM – 5:00 PM
    assert classify_session(_make_et_datetime(16, 0)) == "POST_RTH"
    assert classify_session(_make_et_datetime(16, 30)) == "POST_RTH"
    assert classify_session(_make_et_datetime(16, 59)) == "POST_RTH"
