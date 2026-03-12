"""
Tests for Safety Rails -- Circuit Breakers
============================================
Covers:
  - MaxDailyLossCircuitBreaker: trip threshold, check, reset
  - MaxConsecutiveLossesBreaker: trip on N losses, win resets count
  - MaxPositionSizeGuard: clamp at max, allow under max
  - HeartbeatMonitor: alert timeout, halt timeout, market hours gating
  - SafetyRails manager: check_all, record_trade, reset_breaker
"""

import tempfile
import time
import pytest

from execution.safety_rails import (
    MaxDailyLossCircuitBreaker,
    MaxConsecutiveLossesBreaker,
    MaxPositionSizeGuard,
    HeartbeatMonitor,
    SafetyRails,
    SafetyRailsConfig,
    SafetyRailEventLog,
)


# =====================================================================
#  MAX DAILY LOSS CIRCUIT BREAKER
# =====================================================================
class TestMaxDailyLossCircuitBreaker:

    def test_not_tripped_initially(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        assert not breaker.is_tripped
        assert breaker.check() is True
        assert breaker.daily_pnl == 0.0

    def test_single_loss_under_threshold(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        tripped = breaker.update_pnl(-200.0)
        assert not tripped
        assert not breaker.is_tripped
        assert breaker.check() is True
        assert breaker.daily_pnl == -200.0

    def test_trips_at_threshold(self):
        """Trips when daily PnL drops to exactly -max_daily_loss."""
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        tripped = breaker.update_pnl(-500.0)
        assert tripped
        assert breaker.is_tripped
        assert breaker.check() is False

    def test_trips_below_threshold(self):
        """Trips when daily PnL drops below -max_daily_loss."""
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-300.0)
        tripped = breaker.update_pnl(-250.0)  # total: -550
        assert tripped
        assert breaker.is_tripped
        assert breaker.daily_pnl == -550.0

    def test_accumulates_losses(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-100.0)
        breaker.update_pnl(-100.0)
        breaker.update_pnl(-100.0)
        assert not breaker.is_tripped
        assert breaker.daily_pnl == -300.0

        breaker.update_pnl(-200.0)  # total: -500
        assert breaker.is_tripped

    def test_wins_offset_losses(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-300.0)
        breaker.update_pnl(100.0)   # net: -200
        breaker.update_pnl(-200.0)  # net: -400
        assert not breaker.is_tripped
        assert breaker.daily_pnl == -400.0

    def test_already_tripped_returns_false(self):
        """Once tripped, update_pnl returns False (already halted)."""
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=100.0)
        breaker.update_pnl(-100.0)
        assert breaker.is_tripped
        result = breaker.update_pnl(-50.0)
        assert result is False  # Already tripped

    def test_manual_reset(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-600.0)
        assert breaker.is_tripped
        assert breaker.check() is False

        breaker.reset()
        assert not breaker.is_tripped
        assert breaker.check() is True
        assert breaker.daily_pnl == 0.0

    def test_reset_daily_clears_pnl_but_not_trip(self):
        """reset_daily clears PnL but does NOT clear a trip."""
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-600.0)
        assert breaker.is_tripped

        breaker.reset_daily()
        # Still tripped -- requires manual reset
        assert breaker.is_tripped

    def test_reset_daily_when_not_tripped(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-200.0)
        breaker.reset_daily()
        assert breaker.daily_pnl == 0.0
        assert not breaker.is_tripped

    def test_status(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=500.0)
        breaker.update_pnl(-200.0)
        status = breaker.get_status()
        assert status["breaker"] == "MaxDailyLoss"
        assert status["tripped"] is False
        assert status["daily_pnl"] == -200.0
        assert status["threshold"] == -500.0

    def test_configurable_threshold(self):
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=300.0)
        breaker.update_pnl(-300.0)
        assert breaker.is_tripped

    def test_event_logging(self):
        tmpdir = tempfile.mkdtemp()
        event_log = SafetyRailEventLog(tmpdir)
        breaker = MaxDailyLossCircuitBreaker(max_daily_loss=100.0, event_log=event_log)
        breaker.update_pnl(-100.0)
        assert breaker.is_tripped

        import os
        log_path = os.path.join(tmpdir, "safety_rail_events.json")
        assert os.path.exists(log_path)


# =====================================================================
#  MAX CONSECUTIVE LOSSES BREAKER
# =====================================================================
class TestMaxConsecutiveLossesBreaker:

    def test_not_tripped_initially(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=5)
        assert not breaker.is_tripped
        assert breaker.check() is True
        assert breaker.consecutive_losses == 0

    def test_single_loss_under_threshold(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=5)
        tripped = breaker.record_trade(-50.0)
        assert not tripped
        assert breaker.consecutive_losses == 1

    def test_trips_at_threshold(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=5)
        for i in range(4):
            tripped = breaker.record_trade(-50.0)
            assert not tripped
        tripped = breaker.record_trade(-50.0)  # 5th loss
        assert tripped
        assert breaker.is_tripped
        assert breaker.check() is False

    def test_win_resets_counter(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=5)
        breaker.record_trade(-50.0)
        breaker.record_trade(-50.0)
        breaker.record_trade(-50.0)
        assert breaker.consecutive_losses == 3

        breaker.record_trade(100.0)  # Win resets
        assert breaker.consecutive_losses == 0
        assert not breaker.is_tripped

    def test_alternating_wins_losses_never_trips(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=5)
        for _ in range(100):
            breaker.record_trade(-50.0)
            breaker.record_trade(100.0)
        assert not breaker.is_tripped
        assert breaker.consecutive_losses == 0

    def test_manual_reset(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=3)
        for _ in range(3):
            breaker.record_trade(-50.0)
        assert breaker.is_tripped

        breaker.reset()
        assert not breaker.is_tripped
        assert breaker.consecutive_losses == 0
        assert breaker.check() is True

    def test_already_tripped_returns_false(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=2)
        breaker.record_trade(-50.0)
        breaker.record_trade(-50.0)
        assert breaker.is_tripped

        result = breaker.record_trade(-50.0)
        assert result is False  # Already tripped

    def test_zero_pnl_does_not_count_as_loss(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=3)
        breaker.record_trade(-50.0)
        breaker.record_trade(0.0)  # breakeven = win (resets)
        breaker.record_trade(-50.0)
        assert breaker.consecutive_losses == 1

    def test_status(self):
        breaker = MaxConsecutiveLossesBreaker(max_consecutive=5)
        breaker.record_trade(-50.0)
        breaker.record_trade(-50.0)
        status = breaker.get_status()
        assert status["breaker"] == "MaxConsecutiveLosses"
        assert status["consecutive_losses"] == 2
        assert status["threshold"] == 5


# =====================================================================
#  MAX POSITION SIZE GUARD
# =====================================================================
class TestMaxPositionSizeGuard:

    def test_under_max_passes_through(self):
        guard = MaxPositionSizeGuard(max_contracts=2)
        assert guard.clamp(1) == 1
        assert guard.clamp(2) == 2

    def test_caps_at_max(self):
        """Position size > 2 is clamped to 2. ABSOLUTE -- no exceptions."""
        guard = MaxPositionSizeGuard(max_contracts=2)
        assert guard.clamp(3) == 2
        assert guard.clamp(5) == 2
        assert guard.clamp(10) == 2

    def test_caps_at_custom_max(self):
        guard = MaxPositionSizeGuard(max_contracts=1)
        assert guard.clamp(2) == 1
        assert guard.clamp(1) == 1

    def test_clamp_count_tracked(self):
        guard = MaxPositionSizeGuard(max_contracts=2)
        guard.clamp(1)  # No clamp
        guard.clamp(3)  # Clamped
        guard.clamp(4)  # Clamped
        guard.clamp(2)  # No clamp
        status = guard.get_status()
        assert status["clamp_count"] == 2

    def test_check_method(self):
        guard = MaxPositionSizeGuard(max_contracts=2)
        assert guard.check(1) is True
        assert guard.check(2) is True
        assert guard.check(3) is False

    def test_zero_contracts(self):
        guard = MaxPositionSizeGuard(max_contracts=2)
        assert guard.clamp(0) == 0

    def test_modifiers_cannot_override(self):
        """Even if modifiers calculate higher, hard cap at 2."""
        guard = MaxPositionSizeGuard(max_contracts=2)
        # Simulate modifier calculating 4 contracts
        modifier_calculated = 4
        assert guard.clamp(modifier_calculated) == 2


# =====================================================================
#  HEARTBEAT MONITOR
# =====================================================================
class TestHeartbeatMonitor:

    def test_not_tripped_initially(self):
        monitor = HeartbeatMonitor(alert_seconds=60.0, halt_seconds=300.0)
        assert not monitor.is_tripped
        assert monitor.check() is True

    def test_not_tripped_outside_market_hours(self):
        """Heartbeat only triggers during market hours."""
        monitor = HeartbeatMonitor(alert_seconds=0.01, halt_seconds=0.02)
        monitor.set_market_hours(False)
        time.sleep(0.05)
        assert monitor.check() is True  # Outside market hours = OK

    def test_alert_during_market_hours(self):
        """Alert fires after alert_seconds during market hours."""
        monitor = HeartbeatMonitor(alert_seconds=0.01, halt_seconds=10.0)
        monitor.set_market_hours(True)
        time.sleep(0.02)
        # Should still be allowed (not halt threshold) but alert sent
        assert monitor.check() is True  # Under halt threshold
        assert monitor._alert_sent is True

    def test_halt_during_market_hours(self):
        """Halt fires after halt_seconds during market hours."""
        monitor = HeartbeatMonitor(alert_seconds=0.01, halt_seconds=0.02)
        monitor.set_market_hours(True)
        time.sleep(0.05)
        assert monitor.check() is False
        assert monitor.is_tripped

    def test_heartbeat_resets_timer(self):
        monitor = HeartbeatMonitor(alert_seconds=0.05, halt_seconds=0.10)
        monitor.set_market_hours(True)
        # Send heartbeats to keep alive
        for _ in range(5):
            time.sleep(0.02)
            monitor.heartbeat()
        # Should still be OK
        assert monitor.check() is True
        assert not monitor.is_tripped

    def test_manual_reset(self):
        monitor = HeartbeatMonitor(alert_seconds=0.01, halt_seconds=0.02)
        monitor.set_market_hours(True)
        time.sleep(0.05)
        monitor.check()
        assert monitor.is_tripped

        monitor.reset()
        assert not monitor.is_tripped
        assert monitor.check() is True

    def test_seconds_since_last(self):
        monitor = HeartbeatMonitor()
        time.sleep(0.05)
        assert monitor.seconds_since_last >= 0.04

        monitor.heartbeat()
        assert monitor.seconds_since_last < 0.02

    def test_status(self):
        monitor = HeartbeatMonitor(alert_seconds=60.0, halt_seconds=300.0)
        monitor.set_market_hours(True)
        status = monitor.get_status()
        assert status["breaker"] == "Heartbeat"
        assert status["tripped"] is False
        assert status["alert_threshold"] == 60.0
        assert status["halt_threshold"] == 300.0
        assert status["is_market_hours"] is True


# =====================================================================
#  SAFETY RAILS MANAGER
# =====================================================================
class TestSafetyRails:

    def test_all_ok_initially(self):
        rails = SafetyRails(SafetyRailsConfig(log_dir=tempfile.mkdtemp()))
        assert rails.check_all() is True
        status = rails.get_status()
        assert status["trading_allowed"] is True
        assert status["any_tripped"] is False

    def test_daily_loss_trips_check_all(self):
        rails = SafetyRails(SafetyRailsConfig(
            max_daily_loss=100.0,
            log_dir=tempfile.mkdtemp(),
        ))
        rails.record_trade(-100.0)
        assert rails.check_all() is False

    def test_consecutive_losses_trips_check_all(self):
        rails = SafetyRails(SafetyRailsConfig(
            max_consecutive_losses=3,
            log_dir=tempfile.mkdtemp(),
        ))
        rails.record_trade(-50.0)
        rails.record_trade(-50.0)
        rails.record_trade(-50.0)
        assert rails.check_all() is False

    def test_record_trade_updates_both_breakers(self):
        rails = SafetyRails(SafetyRailsConfig(log_dir=tempfile.mkdtemp()))
        rails.record_trade(-100.0)
        assert rails.daily_loss.daily_pnl == -100.0
        assert rails.consecutive_losses.consecutive_losses == 1

    def test_win_resets_consecutive_but_not_daily(self):
        rails = SafetyRails(SafetyRailsConfig(log_dir=tempfile.mkdtemp()))
        rails.record_trade(-100.0)
        rails.record_trade(50.0)
        assert rails.consecutive_losses.consecutive_losses == 0
        assert rails.daily_loss.daily_pnl == -50.0

    def test_clamp_position_size(self):
        rails = SafetyRails(SafetyRailsConfig(
            max_position_size=2,
            log_dir=tempfile.mkdtemp(),
        ))
        assert rails.clamp_position_size(1) == 1
        assert rails.clamp_position_size(2) == 2
        assert rails.clamp_position_size(5) == 2

    def test_reset_breaker_by_name(self):
        rails = SafetyRails(SafetyRailsConfig(
            max_daily_loss=100.0,
            log_dir=tempfile.mkdtemp(),
        ))
        rails.record_trade(-100.0)
        assert not rails.check_all()

        result = rails.reset_breaker("daily_loss")
        assert result is True
        assert rails.check_all() is True

    def test_reset_breaker_invalid_name(self):
        rails = SafetyRails(SafetyRailsConfig(log_dir=tempfile.mkdtemp()))
        result = rails.reset_breaker("nonexistent")
        assert result is False

    def test_reset_all(self):
        rails = SafetyRails(SafetyRailsConfig(
            max_daily_loss=100.0,
            max_consecutive_losses=2,
            log_dir=tempfile.mkdtemp(),
        ))
        rails.record_trade(-100.0)
        rails.record_trade(-50.0)
        assert not rails.check_all()

        rails.reset_all()
        assert rails.check_all() is True

    def test_heartbeat_integration(self):
        rails = SafetyRails(SafetyRailsConfig(
            heartbeat_alert_seconds=0.01,
            heartbeat_halt_seconds=0.02,
            log_dir=tempfile.mkdtemp(),
        ))
        rails.set_market_hours(True)
        time.sleep(0.05)
        assert rails.check_all() is False  # Heartbeat tripped

    def test_on_bar_received_updates_heartbeat(self):
        rails = SafetyRails(SafetyRailsConfig(
            heartbeat_alert_seconds=0.05,
            heartbeat_halt_seconds=0.10,
            log_dir=tempfile.mkdtemp(),
        ))
        rails.set_market_hours(True)
        for _ in range(5):
            time.sleep(0.02)
            rails.on_bar_received()
        assert rails.check_all() is True

    def test_status_comprehensive(self):
        rails = SafetyRails(SafetyRailsConfig(log_dir=tempfile.mkdtemp()))
        status = rails.get_status()
        assert "daily_loss" in status
        assert "consecutive_losses" in status
        assert "position_size" in status
        assert "heartbeat" in status
        assert "trading_allowed" in status
        assert "any_tripped" in status


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
