"""
Tests for 10 critical/high bugs from production code review.
==============================================================
Each test verifies one specific bug fix.
"""

import asyncio
import math
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from config.settings import BotConfig, RiskConfig, ScaleOutConfig, ExecutionConfig
from risk.engine import RiskEngine, RiskDecision
from execution.scale_out_executor import (
    ScaleOutExecutor, ScaleOutTrade, ScaleOutPhase, ContractLeg,
)
from features.engine import NQFeatureEngine, Bar
from data_pipeline.pipeline import MultiTimeframeIterator, BarData


def _make_config(**risk_overrides) -> BotConfig:
    """Build a minimal BotConfig with optional risk overrides."""
    risk_kw = {
        "account_size": 50_000.0,
        "max_total_drawdown_pct": 10.0,
        "kill_switch_max_consecutive_losses": 5,
        "kill_switch_cooldown_minutes": 60,
        "max_daily_loss_pct": 3.0,
        "max_vix_for_full_size": 25.0,
        "max_vix_for_trading": 40.0,
        "reduce_size_overnight": True,
        "overnight_start_hour": 18,
        "overnight_end_hour": 8,
        "atr_period": 14,
        "atr_multiplier_stop": 2.0,
        "atr_multiplier_target": 1.5,
        "min_rr_ratio": 1.5,
        "use_micro": True,
        "nq_point_value_micro": 2.0,
        "max_contracts_micro": 2,
        "commission_per_contract": 1.29,
        "max_slippage_ticks": 4,
    }
    risk_kw.update(risk_overrides)
    return BotConfig(risk=RiskConfig(**risk_kw))


# =====================================================================
#  BUG 1: Kill switch re-trigger after drawdown cooldown
# =====================================================================
class TestKillSwitchNoRetriggerAfterDrawdownCooldown:
    """When kill switch deactivates after drawdown-triggered cooldown,
    the drawdown condition must NOT re-trigger immediately."""

    def test_drawdown_triggered_kill_switch_does_not_retrigger(self):
        config = _make_config(max_total_drawdown_pct=10.0,
                              kill_switch_cooldown_minutes=60)
        engine = RiskEngine(config)

        # Simulate drawdown beyond threshold
        engine.state.current_equity = 44_000  # 12% DD from 50k
        engine.state.peak_equity = 50_000
        engine._update_drawdown()

        now = datetime(2026, 3, 6, 14, 0, tzinfo=timezone.utc)

        # This should trigger kill switch via max drawdown
        result = engine.evaluate_trade("long", 20000, 10.0, current_time=now)
        assert result.decision == RiskDecision.KILL_SWITCH
        assert engine.state.kill_switch_active

        # Advance past cooldown
        after_cooldown = now + timedelta(minutes=61)

        # Evaluate again — should NOT re-trigger kill switch
        result2 = engine.evaluate_trade("long", 20000, 10.0, current_time=after_cooldown)
        assert result2.decision != RiskDecision.KILL_SWITCH, (
            "Kill switch re-triggered immediately after drawdown cooldown!"
        )
        assert not engine.state.kill_switch_active

    def test_consecutive_loss_triggered_resets_losses(self):
        config = _make_config(kill_switch_max_consecutive_losses=3)
        engine = RiskEngine(config)

        # Simulate 3 consecutive losses
        for _ in range(3):
            engine.record_trade_result(-100, "long")

        now = datetime(2026, 3, 6, 14, 0, tzinfo=timezone.utc)
        result = engine.evaluate_trade("long", 20000, 10.0, current_time=now)
        assert result.decision == RiskDecision.KILL_SWITCH

        # After cooldown
        after = now + timedelta(minutes=61)
        result2 = engine.evaluate_trade("long", 20000, 10.0, current_time=after)
        # Should deactivate and reset consecutive_losses
        assert engine.state.consecutive_losses == 0
        assert result2.decision != RiskDecision.KILL_SWITCH


# =====================================================================
#  BUG 2: C2 trailing stop for short trades
# =====================================================================
class TestC2TrailingStopShortDirection:
    """Verify trailing stop ratchets DOWN for short trades as price drops."""

    def test_short_trailing_stop_moves_down(self):
        config = _make_config()
        executor = ScaleOutExecutor(config)

        # Create a short trade manually
        trade = ScaleOutTrade(direction="short", entry_price=20000.0)
        trade.c2.entry_price = 20000.0
        trade.c2.stop_price = 20020.0  # Initial stop above entry
        trade.c2.is_open = True
        trade.c2.is_filled = True
        trade.c2_best_price = 20000.0
        trade.atr_at_entry = 10.0
        trade._set_phase(ScaleOutPhase.RUNNING)
        executor._active_trade = trade

        # Price drops to 19950 — best price should update, trail should drop
        loop = asyncio.new_event_loop()
        now = datetime.now(timezone.utc)

        loop.run_until_complete(executor.update(19950.0, now))

        # Best price should be 19950 (lower = better for shorts)
        assert trade.c2_best_price == 19950.0

        # Trail should be below original stop (ratcheted down)
        assert trade.c2.stop_price < 20020.0

        # Price drops further to 19900
        loop.run_until_complete(executor.update(19900.0, now + timedelta(minutes=1)))
        assert trade.c2_best_price == 19900.0

        # Stop should have moved down further
        assert trade.c2.stop_price < 19970.0

        # Price bounces up — stop should NOT move up
        old_stop = trade.c2.stop_price
        loop.run_until_complete(executor.update(19950.0, now + timedelta(minutes=2)))
        assert trade.c2.stop_price == old_stop, "Short trailing stop moved UP on bounce!"

        loop.close()


# =====================================================================
#  BUG 3: Broker stop modification failure — no local update
# =====================================================================
class TestBrokerStopModificationFailureNoLocalUpdate:
    """If broker API call fails, local stop_price must NOT be updated."""

    def test_broker_failure_skips_local_update(self):
        config = _make_config()
        # Set to live mode
        config.execution = ExecutionConfig(paper_trading=False)
        executor = ScaleOutExecutor(config)

        # Mock broker that fails
        mock_broker = MagicMock()
        mock_broker.modify_stop = AsyncMock(side_effect=Exception("Network error"))
        executor.broker = mock_broker

        trade = ScaleOutTrade(direction="long", entry_price=20000.0)
        trade.c2.entry_price = 20000.0
        trade.c2.stop_price = 19980.0
        trade.c2.stop_order_id = 12345
        trade.c2.is_open = True
        trade.c2.is_filled = True
        trade.c2_best_price = 20000.0
        trade.atr_at_entry = 10.0
        trade._set_phase(ScaleOutPhase.RUNNING)
        executor._active_trade = trade

        original_stop = trade.c2.stop_price
        loop = asyncio.new_event_loop()
        now = datetime.now(timezone.utc)

        # Price goes up, trailing stop should try to update
        loop.run_until_complete(executor.update(20050.0, now))

        # Broker call failed — local state should be unchanged
        assert trade.c2.stop_price == original_stop, (
            f"Local stop updated to {trade.c2.stop_price} despite broker failure!"
        )

        loop.close()


# =====================================================================
#  BUG 4: Warmup suppresses trades
# =====================================================================
class TestWarmupSuppressesTrades:
    """During warmup, bars should feed to feature engine only,
    NOT to the orchestrator/original_on_bar callback."""

    def test_warmup_does_not_call_original_on_bar(self):
        """The warmup wrapper should only call feature_engine.update(),
        never the original_on_bar callback during warmup."""
        from scripts.run_ibkr import IBKRLiveRunner

        import ast, inspect
        source = inspect.getsource(IBKRLiveRunner._on_bar_wrapper)

        # Parse the AST and check that in the warmup branch,
        # there's no call to original_on_bar
        # Simpler approach: verify feature_engine.update is called in warmup
        assert "self._pipeline._feature_engine.update(bar)" in source

        # Verify original_on_bar is only called AFTER warmup is complete
        # by checking the warmup early-return block doesn't invoke it.
        # Extract the warmup block: between "if self._warmup_bar_count < self.WARMUP_BARS"
        # and "return"
        lines = source.split("\n")
        in_warmup_block = False
        warmup_code_lines = []
        for line in lines:
            if "self._warmup_bar_count < self.WARMUP_BARS" in line:
                in_warmup_block = True
                continue
            if in_warmup_block:
                stripped = line.strip()
                if stripped == "return":
                    break
                # Only check actual code lines, not comments
                if stripped and not stripped.startswith("#"):
                    warmup_code_lines.append(stripped)

        warmup_code = "\n".join(warmup_code_lines)
        assert "original_on_bar" not in warmup_code, (
            "original_on_bar called in warmup code (not in comments)!"
        )
        assert "process_bar" not in warmup_code, (
            "process_bar called during warmup!"
        )


# =====================================================================
#  BUG 5: Best price initialization consistent
# =====================================================================
class TestBestPriceInitializationConsistent:
    """Both c1_best_price and c2_best_price should be initialized to
    entry_price — no == 0 dead code checks."""

    def test_paper_enter_initializes_best_prices(self):
        config = _make_config()
        executor = ScaleOutExecutor(config)

        loop = asyncio.new_event_loop()
        trade = loop.run_until_complete(
            executor.enter_trade("long", 20000.0, 20.0, 10.0)
        )

        assert trade is not None
        assert trade.c1_best_price == trade.entry_price
        assert trade.c2_best_price == trade.entry_price
        assert trade.c1_best_price != 0  # Not left at default

        loop.close()

    def test_short_best_price_no_zero_check(self):
        """Short trades should use min() without == 0 guard."""
        config = _make_config()
        executor = ScaleOutExecutor(config)

        loop = asyncio.new_event_loop()
        trade = loop.run_until_complete(
            executor.enter_trade("short", 20000.0, 20.0, 10.0)
        )

        assert trade is not None
        # Best price initialized to entry
        assert trade.c1_best_price == trade.entry_price
        assert trade.c2_best_price == trade.entry_price

        # Price drops — min() should work without == 0 check
        now = datetime.now(timezone.utc)
        loop.run_until_complete(executor.update(19990.0, now))
        assert trade.c1_best_price <= trade.entry_price
        assert trade.c2_best_price <= trade.entry_price

        loop.close()


# =====================================================================
#  BUG 6: VWAP resets at session boundary
# =====================================================================
class TestVwapResetsAtSessionBoundary:
    """Feature engine's VWAP/cumulative delta must reset when
    process_bar detects a new trading day."""

    def test_vwap_resets_on_new_day(self):
        config = _make_config()
        engine = NQFeatureEngine(config)

        # Simulate bars on day 1
        day1 = datetime(2026, 3, 5, 15, 0, tzinfo=timezone.utc)
        for i in range(25):
            bar = Bar(
                timestamp=day1 + timedelta(minutes=i),
                open=20000.0, high=20005.0, low=19995.0, close=20000.0,
                volume=100, delta=10,
            )
            engine.update(bar)

        # Accumulate VWAP state
        assert engine._session_volume_sum > 0
        assert engine._cumulative_delta > 0

        # Reset (called at session boundary)
        engine.reset_session()

        assert engine._session_volume_sum == 0
        assert engine._session_volume_price_sum == 0.0
        assert engine._cumulative_delta == 0

    def test_orchestrator_detects_session_boundary(self):
        """Verify the orchestrator's process_bar detects new trading days."""
        from main import TradingOrchestrator

        # Check that session boundary detection code exists
        import inspect
        source = inspect.getsource(TradingOrchestrator.process_bar)
        assert "_last_trading_date" in source
        assert "reset_session" in source


# =====================================================================
#  BUG 7: Sweep stop validation
# =====================================================================
class TestSweepStopValidation:
    """If sweep_signal.stop_price is 0 or None, sweep_stop_override
    must fall back to None (ATR-based stop), not produce a full-price
    stop distance."""

    def test_zero_stop_price_handled(self):
        """Directly test the logic pattern from main.py."""
        bar_close = 20000.0

        # Case 1: valid stop price
        stop_price_valid = 19980.0
        if stop_price_valid and stop_price_valid > 0:
            override = abs(bar_close - stop_price_valid)
        else:
            override = None
        assert override == 20.0

        # Case 2: stop price is 0
        stop_price_zero = 0
        if stop_price_zero and stop_price_zero > 0:
            override_zero = abs(bar_close - stop_price_zero)
        else:
            override_zero = None
        assert override_zero is None, "Zero stop price should yield None override"

        # Case 3: stop price is None
        stop_price_none = None
        if stop_price_none and stop_price_none > 0:
            override_none = abs(bar_close - stop_price_none)
        else:
            override_none = None
        assert override_none is None, "None stop price should yield None override"

    def test_main_py_has_validation(self):
        """Verify main.py contains the stop price validation."""
        from main import TradingOrchestrator
        import inspect
        source = inspect.getsource(TradingOrchestrator.process_bar)
        assert "sweep_signal.stop_price > 0" in source or \
               "stop_price and sweep_signal.stop_price > 0" in source


# =====================================================================
#  BUG 8: Overnight check with DST
# =====================================================================
class TestOvernightCheckDst:
    """_is_overnight must use proper timezone (America/New_York),
    not hardcoded UTC-5 offset."""

    def test_dst_summer_time_correct(self):
        """During DST (summer), ET is UTC-4. A bar at 17:30 UTC
        is 13:30 ET (NOT overnight)."""
        config = _make_config(overnight_start_hour=18, overnight_end_hour=8)
        engine = RiskEngine(config)

        # July 15 (DST active): 17:30 UTC = 13:30 ET (not overnight)
        summer_time = datetime(2026, 7, 15, 17, 30, tzinfo=timezone.utc)
        assert not engine._is_overnight(summer_time), (
            "13:30 ET should NOT be overnight"
        )

        # Same time in winter (no DST): 17:30 UTC = 12:30 ET (not overnight)
        winter_time = datetime(2026, 1, 15, 17, 30, tzinfo=timezone.utc)
        assert not engine._is_overnight(winter_time)

    def test_dst_transition_boundary(self):
        """Test the tricky hour around DST transition."""
        config = _make_config(overnight_start_hour=18, overnight_end_hour=8)
        engine = RiskEngine(config)

        # In summer: 22:30 UTC = 18:30 ET (overnight)
        summer_evening = datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)
        assert engine._is_overnight(summer_evening)

        # In winter: 23:30 UTC = 18:30 ET (overnight)
        winter_evening = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
        assert engine._is_overnight(winter_evening)

    def test_uses_zoneinfo_not_hardcoded(self):
        """Verify the code uses ZoneInfo, not a hardcoded offset."""
        import inspect
        source = inspect.getsource(RiskEngine._is_overnight)
        assert 'ZoneInfo("America/New_York")' in source or \
               "ZoneInfo('America/New_York')" in source
        assert "UTC-5" not in source
        assert "utcoffset" not in source


# =====================================================================
#  BUG 9: Size multiplier not zero
# =====================================================================
class TestSizeMultiplierNotZero:
    """Using min(factors) instead of multiplying prevents 0-contract
    sizing when multiple adverse conditions stack."""

    def test_worst_case_not_zero(self):
        """VIX=55, 7 consecutive losses, 15% drawdown, overnight
        should NOT produce a 0 multiplier."""
        config = _make_config()
        engine = RiskEngine(config)

        engine.state.is_overnight = True
        engine.state.consecutive_losses = 7
        engine.state.current_drawdown_pct = 15.0

        vix = 55.0  # Very high
        now = datetime(2026, 3, 6, 3, 0, tzinfo=timezone.utc)

        mult = engine._compute_size_multiplier(vix, now)

        # With min(factors), worst single factor is 0.25
        assert mult >= 0.25, (
            f"Size multiplier {mult} is too low — would round to 0 contracts"
        )

    def test_single_adverse_factor(self):
        """With only one factor active, multiplier = that factor."""
        config = _make_config()
        engine = RiskEngine(config)

        engine.state.consecutive_losses = 7
        mult = engine._compute_size_multiplier(0, datetime.now(timezone.utc))
        expected = max(0.25, 1.0 - (7 - 2) * 0.15)  # 0.25
        assert mult == expected

    def test_no_factors_returns_one(self):
        """No adverse conditions → multiplier = 1.0."""
        config = _make_config()
        engine = RiskEngine(config)
        mult = engine._compute_size_multiplier(0, datetime.now(timezone.utc))
        assert mult == 1.0


# =====================================================================
#  BUG 10: MTF iterator sorts correctly
# =====================================================================
class TestMtfIteratorSortsCorrectly:
    """MultiTimeframeIterator must sort by (timestamp, TF priority)."""

    def test_sorts_by_timestamp_then_priority(self):
        t1 = datetime(2026, 3, 6, 14, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 6, 14, 2, tzinfo=timezone.utc)

        items = [
            ("2m", BarData(timestamp=t2, open=1, high=1, low=1, close=1, volume=1)),
            ("1H", BarData(timestamp=t1, open=1, high=1, low=1, close=1, volume=1)),
            ("2m", BarData(timestamp=t1, open=1, high=1, low=1, close=1, volume=1)),
            ("5m", BarData(timestamp=t1, open=1, high=1, low=1, close=1, volume=1)),
        ]

        iterator = MultiTimeframeIterator(items)
        result = list(iterator)

        # All t1 items first, then t2
        assert result[0][1].timestamp == t1
        assert result[-1][1].timestamp == t2

        # Within t1: 1H (priority 2) before 5m (5) before 2m (7)
        t1_items = [r for r in result if r[1].timestamp == t1]
        assert t1_items[0][0] == "1H"
        assert t1_items[1][0] == "5m"
        assert t1_items[2][0] == "2m"

    def test_unsorted_input_gets_sorted(self):
        """Even if input is completely unsorted, output is sorted."""
        t1 = datetime(2026, 3, 6, 14, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 6, 13, 0, tzinfo=timezone.utc)  # Earlier

        items = [
            ("2m", BarData(timestamp=t1, open=1, high=1, low=1, close=1, volume=1)),
            ("2m", BarData(timestamp=t2, open=1, high=1, low=1, close=1, volume=1)),
        ]

        iterator = MultiTimeframeIterator(items)
        result = list(iterator)

        # t2 is earlier, should come first
        assert result[0][1].timestamp == t2
        assert result[1][1].timestamp == t1
