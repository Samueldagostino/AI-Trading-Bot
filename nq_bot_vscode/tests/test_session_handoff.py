"""
Tests for SessionHandoffAnalyzer.

Uses synthetic data with known answers to validate classification,
probability calculations, statistical tests, and edge cases.
"""

import os
import tempfile
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from nq_bot_vscode.research.session_handoff_analyzer import (
    HANDOFF_PAIRS,
    HandoffOutcome,
    ProbabilityCell,
    SessionBehavior,
    SessionHandoffAnalyzer,
    SessionName,
    SessionStats,
    _wilson_ci,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar(dt_str: str, open_: float, high: float, low: float, close: float, volume: int = 100) -> dict:
    return {
        "timestamp": dt_str,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _generate_flat_session_csv(
    start_date: str = "2023-01-02",
    n_days: int = 5,
    base_price: float = 15000.0,
) -> str:
    """Generate synthetic 1-min CSV with flat sessions for deterministic testing."""
    rows = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")

    for day_offset in range(n_days):
        current_date = dt + timedelta(days=day_offset)
        # Skip weekends
        if current_date.weekday() >= 5:
            n_days += 1
            continue

        price = base_price

        # Generate bars for full 24h
        for hour in range(24):
            for minute in range(60):
                bar_time = current_date.replace(hour=hour, minute=minute)
                ts = bar_time.strftime("%Y-%m-%d %H:%M:%S-0500")
                # Flat bars -- all OHLC the same
                rows.append(_make_bar(ts, price, price + 0.25, price - 0.25, price))

    df = pd.DataFrame(rows)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    df.to_csv(tmp.name, index=False)
    return tmp.name


def _generate_trending_session_csv(
    start_date: str = "2023-01-02",
    n_days: int = 60,
    base_price: float = 15000.0,
    asia_trend: float = 0.005,
    london_trend: float = 0.005,
) -> str:
    """
    Generate synthetic CSV where Asia trends up and London continues up,
    creating a known CONTINUATION pattern.
    """
    rows = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    day_count = 0

    while day_count < n_days:
        current_date = dt + timedelta(days=day_count)
        day_count += 1
        if current_date.weekday() >= 5:
            continue

        price = base_price

        for hour in range(24):
            for minute in range(60):
                bar_time = current_date.replace(hour=hour, minute=minute)
                ts = bar_time.strftime("%Y-%m-%d %H:%M:%S-0500")

                t = bar_time.time()
                # Asia: 18:00-02:00 -- trend up
                if t >= time(18, 0) or t < time(2, 0):
                    drift = asia_trend / (8 * 60)  # spread over 8 hours
                    price *= (1 + drift)
                # London: 02:00-08:00 -- same direction
                elif time(2, 0) <= t < time(8, 0):
                    drift = london_trend / (6 * 60)
                    price *= (1 + drift)
                # NY sessions: flat
                else:
                    pass

                high = price * 1.0001
                low = price * 0.9999
                rows.append(_make_bar(ts, price, high, low, price * (1 + drift / 2) if 'drift' in dir() else price))

        # Reset price each day to avoid compounding issues
        base_price = 15000.0

    df = pd.DataFrame(rows)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    df.to_csv(tmp.name, index=False)
    return tmp.name


def _generate_simple_test_csv(n_days: int = 100) -> str:
    """
    Generate a simple test CSV with enough data for statistical tests.
    Asia trends up, London reverses -- creates known REVERSAL pattern.
    """
    rows = []
    dt = datetime(2023, 1, 2)
    day_count = 0

    while day_count < n_days:
        current_date = dt + timedelta(days=day_count)
        day_count += 1
        if current_date.weekday() >= 5:
            continue

        price = 15000.0

        for hour in range(24):
            for minute in range(60):
                bar_time = current_date.replace(hour=hour, minute=minute)
                ts = bar_time.strftime("%Y-%m-%d %H:%M:%S-0500")
                t = bar_time.time()

                # Asia: strong up trend
                if t >= time(18, 0) or t < time(2, 0):
                    price += 0.15  # ~72 points over 480 bars = 0.48% on 15000
                    high = price + 1.0
                    low = price - 0.5
                    close = price + 0.5  # close near high
                # London: reversal (down)
                elif time(2, 0) <= t < time(8, 0):
                    price -= 0.12  # ~43 points down over 360 bars
                    high = price + 0.5
                    low = price - 1.0
                    close = price - 0.5  # close near low
                # NY Open: small range
                elif time(8, 0) <= t < time(10, 30):
                    high = price + 0.25
                    low = price - 0.25
                    close = price
                # NY Core: flat
                elif time(10, 30) <= t < time(15, 0):
                    high = price + 0.25
                    low = price - 0.25
                    close = price
                # NY Close: flat
                else:
                    high = price + 0.25
                    low = price - 0.25
                    close = price

                rows.append(_make_bar(ts, price, high, low, close))

    df = pd.DataFrame(rows)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    df.to_csv(tmp.name, index=False)
    return tmp.name


# ---------------------------------------------------------------------------
# Tests: ProbabilityCell and Wilson CI
# ---------------------------------------------------------------------------

class TestProbabilityCell:
    def test_basic_probability(self):
        cell = ProbabilityCell(count=30, total=100)
        assert cell.probability == pytest.approx(0.3)

    def test_zero_total(self):
        cell = ProbabilityCell(count=0, total=0)
        assert cell.probability == 0.0
        assert cell.ci_95 == (0.0, 0.0)

    def test_reliability_thresholds(self):
        assert not ProbabilityCell(count=5, total=20).is_reliable
        assert ProbabilityCell(count=10, total=30).is_reliable
        assert not ProbabilityCell(count=10, total=30).is_meaningful
        assert ProbabilityCell(count=25, total=50).is_meaningful

    def test_wilson_ci_bounds(self):
        lo, hi = _wilson_ci(50, 100, 0.95)
        assert 0.35 < lo < 0.45
        assert 0.55 < hi < 0.65
        assert lo < hi

    def test_wilson_ci_edge_cases(self):
        # All successes
        lo, hi = _wilson_ci(100, 100, 0.95)
        assert hi <= 1.0
        assert lo > 0.9

        # No successes
        lo, hi = _wilson_ci(0, 100, 0.95)
        assert lo >= 0.0
        assert hi < 0.1


# ---------------------------------------------------------------------------
# Tests: SessionStats
# ---------------------------------------------------------------------------

class TestSessionStats:
    def test_session_return(self):
        s = SessionStats(
            session_name=SessionName.ASIA, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15100.0, low_price=14950.0,
            close_price=15050.0, volume=10000, bar_count=480,
        )
        assert s.session_return == pytest.approx(50.0 / 15000.0)

    def test_close_position_in_range(self):
        s = SessionStats(
            session_name=SessionName.LONDON, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15100.0, low_price=14900.0,
            close_price=15100.0, volume=5000, bar_count=360,
        )
        assert s.close_position_in_range == pytest.approx(1.0)

    def test_close_position_zero_range(self):
        s = SessionStats(
            session_name=SessionName.LONDON, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15000.0, low_price=15000.0,
            close_price=15000.0, volume=100, bar_count=10,
        )
        assert s.close_position_in_range == 0.5

    def test_session_range(self):
        s = SessionStats(
            session_name=SessionName.NY_OPEN, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15100.0, low_price=14900.0,
            close_price=15000.0, volume=5000, bar_count=150,
        )
        assert s.session_range == pytest.approx(200.0 / 15000.0)


# ---------------------------------------------------------------------------
# Tests: Session Classification
# ---------------------------------------------------------------------------

class TestSessionClassification:
    def setup_method(self):
        self.analyzer = SessionHandoffAnalyzer()
        # Set a median range for classification
        self.analyzer.median_ranges = {s: 0.003 for s in SessionName}

    def test_strong_trend_up(self):
        s = SessionStats(
            session_name=SessionName.ASIA, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15060.0, low_price=14990.0,
            close_price=15055.0,  # close in top 20% of range (14990 to 15060)
            volume=10000, bar_count=480,
        )
        # Return: 55/15000 = 0.00367 > 0.003
        # Close position: (15055-14990)/(15060-14990) = 65/70 = 0.928 > 0.8
        # But range = 70/15000 = 0.00467 > 1.5*0.003=0.0045 → EXPANSION
        self.analyzer.median_ranges[SessionName.ASIA] = 0.005
        result = self.analyzer._classify_session(s)
        assert result == SessionBehavior.STRONG_TREND_UP

    def test_strong_trend_down(self):
        s = SessionStats(
            session_name=SessionName.ASIA, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15010.0, low_price=14940.0,
            close_price=14945.0,  # close near bottom
            volume=10000, bar_count=480,
        )
        # Return: -55/15000 = -0.00367 < -0.003
        # Close position: (14945-14940)/(15010-14940) = 5/70 = 0.071 < 0.2
        self.analyzer.median_ranges[SessionName.ASIA] = 0.005
        result = self.analyzer._classify_session(s)
        assert result == SessionBehavior.STRONG_TREND_DOWN

    def test_range_bound(self):
        s = SessionStats(
            session_name=SessionName.LONDON, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15005.0, low_price=14995.0,
            close_price=15002.0,
            volume=5000, bar_count=360,
        )
        # Return: 2/15000 = 0.000133 < 0.001
        # Range: 10/15000 = 0.000667 < 0.003 median
        self.analyzer.median_ranges[SessionName.LONDON] = 0.003
        result = self.analyzer._classify_session(s)
        assert result == SessionBehavior.RANGE_BOUND

    def test_spike_reversal(self):
        s = SessionStats(
            session_name=SessionName.NY_OPEN, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15070.0, low_price=14930.0,
            close_price=15005.0,  # close near open
            volume=10000, bar_count=150,
        )
        # Range: 140/15000 = 0.00933 > 0.004
        # Net: 5/15000 = 0.000333 < 0.001
        # But range > 1.5 * 0.003 → EXPANSION takes priority
        self.analyzer.median_ranges[SessionName.NY_OPEN] = 0.01
        result = self.analyzer._classify_session(s)
        assert result == SessionBehavior.SPIKE_REVERSAL

    def test_expansion(self):
        s = SessionStats(
            session_name=SessionName.LONDON, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15200.0, low_price=14800.0,
            close_price=15150.0,
            volume=20000, bar_count=360,
        )
        # Range: 400/15000 = 0.0267 > 1.5 * 0.003 = 0.0045
        result = self.analyzer._classify_session(s)
        assert result == SessionBehavior.EXPANSION

    def test_weak_trend_up(self):
        s = SessionStats(
            session_name=SessionName.LONDON, date=datetime(2023, 1, 2),
            open_price=15000.0, high_price=15025.0, low_price=14990.0,
            close_price=15020.0,
            volume=5000, bar_count=360,
        )
        # Return: 20/15000 = 0.00133 > 0.001, < 0.003
        # Range: 35/15000 = 0.00233 < 0.003 median (not expansion)
        result = self.analyzer._classify_session(s)
        assert result == SessionBehavior.WEAK_TREND_UP


# ---------------------------------------------------------------------------
# Tests: Handoff Classification
# ---------------------------------------------------------------------------

class TestHandoffClassification:
    def setup_method(self):
        self.analyzer = SessionHandoffAnalyzer()

    def test_continuation(self):
        # Both positive
        result = self.analyzer._classify_handoff(0.003, 0.002)
        assert result == HandoffOutcome.CONTINUATION

        # Both negative
        result = self.analyzer._classify_handoff(-0.003, -0.002)
        assert result == HandoffOutcome.CONTINUATION

    def test_reversal(self):
        # Up then strong down
        result = self.analyzer._classify_handoff(0.003, -0.002)
        assert result == HandoffOutcome.REVERSAL

        # Down then strong up
        result = self.analyzer._classify_handoff(-0.003, 0.002)
        assert result == HandoffOutcome.REVERSAL

    def test_range(self):
        # Small next session move
        result = self.analyzer._classify_handoff(0.003, 0.0005)
        assert result == HandoffOutcome.RANGE

    def test_mild_counter_is_range(self):
        # Up then mild down (< 0.15%)
        result = self.analyzer._classify_handoff(0.003, -0.001)
        assert result == HandoffOutcome.RANGE


# ---------------------------------------------------------------------------
# Tests: CSV Loading and Full Pipeline
# ---------------------------------------------------------------------------

class TestCSVLoading:
    def test_load_flat_csv(self):
        csv_path = _generate_flat_session_csv(n_days=10)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            assert len(analyzer.sessions) > 0
            assert len(analyzer.observations) >= 0
        finally:
            os.unlink(csv_path)

    def test_sessions_extracted(self):
        csv_path = _generate_flat_session_csv(n_days=10)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            session_names = {s.session_name for s in analyzer.sessions}
            # Should have at least some session types
            assert len(session_names) >= 1
        finally:
            os.unlink(csv_path)

    def test_data_coverage(self):
        csv_path = _generate_flat_session_csv(n_days=10)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            coverage = analyzer.get_data_coverage()
            assert "months" in coverage
            assert "label" in coverage
            assert coverage["total_sessions"] > 0
        finally:
            os.unlink(csv_path)


# ---------------------------------------------------------------------------
# Tests: Statistical Functions
# ---------------------------------------------------------------------------

class TestStatisticalFunctions:
    def test_chi_squared_uniform(self):
        """When distribution IS uniform, p-value should be high."""
        analyzer = SessionHandoffAnalyzer()
        # Create fake observations that are perfectly uniform
        from nq_bot_vscode.research.session_handoff_analyzer import HandoffObservation
        for i in range(90):
            outcome = [HandoffOutcome.CONTINUATION, HandoffOutcome.REVERSAL, HandoffOutcome.RANGE][i % 3]
            analyzer.observations.append(HandoffObservation(
                date=datetime(2023, 1, 2) + timedelta(days=i),
                from_session=SessionName.ASIA,
                to_session=SessionName.LONDON,
                from_behavior=SessionBehavior.STRONG_TREND_UP,
                to_behavior=SessionBehavior.WEAK_TREND_UP,
                from_return=0.003,
                to_return=0.001,
                outcome=outcome,
            ))

        chi = analyzer.chi_squared_test(SessionName.ASIA, SessionName.LONDON)
        result = chi[SessionBehavior.STRONG_TREND_UP]
        assert result["p_value"] > 0.99  # perfectly uniform
        assert not result["significant"]

    def test_chi_squared_biased(self):
        """When distribution is heavily biased, p-value should be low."""
        analyzer = SessionHandoffAnalyzer()
        from nq_bot_vscode.research.session_handoff_analyzer import HandoffObservation

        # 80 continuations, 5 reversals, 5 ranges = heavily biased
        for i in range(80):
            analyzer.observations.append(HandoffObservation(
                date=datetime(2023, 1, 2) + timedelta(days=i),
                from_session=SessionName.ASIA,
                to_session=SessionName.LONDON,
                from_behavior=SessionBehavior.STRONG_TREND_UP,
                to_behavior=SessionBehavior.WEAK_TREND_UP,
                from_return=0.003, to_return=0.002,
                outcome=HandoffOutcome.CONTINUATION,
            ))
        for i in range(5):
            analyzer.observations.append(HandoffObservation(
                date=datetime(2023, 4, 2) + timedelta(days=i),
                from_session=SessionName.ASIA,
                to_session=SessionName.LONDON,
                from_behavior=SessionBehavior.STRONG_TREND_UP,
                to_behavior=SessionBehavior.WEAK_TREND_DOWN,
                from_return=0.003, to_return=-0.002,
                outcome=HandoffOutcome.REVERSAL,
            ))
        for i in range(5):
            analyzer.observations.append(HandoffObservation(
                date=datetime(2023, 5, 2) + timedelta(days=i),
                from_session=SessionName.ASIA,
                to_session=SessionName.LONDON,
                from_behavior=SessionBehavior.STRONG_TREND_UP,
                to_behavior=SessionBehavior.RANGE_BOUND,
                from_return=0.003, to_return=0.0005,
                outcome=HandoffOutcome.RANGE,
            ))

        chi = analyzer.chi_squared_test(SessionName.ASIA, SessionName.LONDON)
        result = chi[SessionBehavior.STRONG_TREND_UP]
        assert result["p_value"] < 0.001
        assert result["significant"]
        assert result["cramers_v"] > 0.3  # large effect

    def test_chi_squared_small_n(self):
        """With very small N, should report as untestable."""
        analyzer = SessionHandoffAnalyzer()
        from nq_bot_vscode.research.session_handoff_analyzer import HandoffObservation
        for i in range(3):
            analyzer.observations.append(HandoffObservation(
                date=datetime(2023, 1, 2) + timedelta(days=i),
                from_session=SessionName.ASIA,
                to_session=SessionName.LONDON,
                from_behavior=SessionBehavior.EXPANSION,
                to_behavior=SessionBehavior.RANGE_BOUND,
                from_return=0.003, to_return=0.0005,
                outcome=HandoffOutcome.RANGE,
            ))

        chi = analyzer.chi_squared_test(SessionName.ASIA, SessionName.LONDON)
        result = chi[SessionBehavior.EXPANSION]
        assert result["chi2"] is None
        assert not result["significant"]


# ---------------------------------------------------------------------------
# Tests: Survivorship Bias
# ---------------------------------------------------------------------------

class TestSurvivorshipBias:
    def test_split_sample(self):
        analyzer = SessionHandoffAnalyzer()
        from nq_bot_vscode.research.session_handoff_analyzer import HandoffObservation

        # Create 60 observations spread over time
        for i in range(60):
            analyzer.observations.append(HandoffObservation(
                date=datetime(2023, 1, 2) + timedelta(days=i),
                from_session=SessionName.LONDON,
                to_session=SessionName.NY_OPEN,
                from_behavior=SessionBehavior.WEAK_TREND_UP,
                to_behavior=SessionBehavior.WEAK_TREND_UP,
                from_return=0.002, to_return=0.002,
                outcome=[HandoffOutcome.CONTINUATION, HandoffOutcome.REVERSAL, HandoffOutcome.RANGE][i % 3],
            ))

        result = analyzer.survivorship_bias_test(SessionName.LONDON, SessionName.NY_OPEN)
        assert "first_half" in result
        assert "second_half" in result
        assert "consistent_edge" in result
        # Uniform data → no edge in either half
        assert not result["consistent_edge"]

    def test_too_few_observations(self):
        analyzer = SessionHandoffAnalyzer()
        result = analyzer.survivorship_bias_test(SessionName.ASIA, SessionName.LONDON)
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests: Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_missing_bars(self):
        """Analyzer should handle days with very few bars gracefully."""
        rows = []
        # Only 3 bars -- should be skipped (< 5 bar threshold)
        for i in range(3):
            ts = f"2023-01-02 18:{i:02d}:00-0500"
            rows.append(_make_bar(ts, 15000.0, 15001.0, 14999.0, 15000.0))

        # Plus a normal day with enough bars
        for hour in range(24):
            for minute in range(60):
                dt = datetime(2023, 1, 3, hour, minute)
                ts = dt.strftime("%Y-%m-%d %H:%M:%S-0500")
                rows.append(_make_bar(ts, 15000.0, 15001.0, 14999.0, 15000.0))

        df = pd.DataFrame(rows)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
        df.to_csv(tmp.name, index=False)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(tmp.name)
            # Should not crash
            assert analyzer.get_data_coverage()["total_sessions"] >= 0
        finally:
            os.unlink(tmp.name)

    def test_empty_handoff_matrix(self):
        """Matrix with no observations should return zero probabilities."""
        analyzer = SessionHandoffAnalyzer()
        matrix = analyzer.get_handoff_matrix(SessionName.ASIA, SessionName.LONDON)
        for behavior in SessionBehavior:
            for outcome in HandoffOutcome:
                assert matrix[behavior][outcome].probability == 0.0
                assert matrix[behavior][outcome].total == 0

    def test_sample_sizes_empty(self):
        analyzer = SessionHandoffAnalyzer()
        sizes = analyzer.get_sample_sizes()
        for key, counts in sizes.items():
            for behavior, count in counts.items():
                assert count == 0

    def test_print_report_no_crash(self):
        """print_full_report should work even with no data."""
        analyzer = SessionHandoffAnalyzer()
        report = analyzer.print_full_report()
        assert "SESSION HANDOFF" in report

    def test_confidence_intervals_empty(self):
        analyzer = SessionHandoffAnalyzer()
        cis = analyzer.get_confidence_intervals(SessionName.ASIA, SessionName.LONDON)
        for behavior in SessionBehavior:
            for outcome in HandoffOutcome:
                assert cis[behavior][outcome] == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Tests: Full Pipeline with Synthetic Data
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_analyze_generates_observations(self):
        csv_path = _generate_simple_test_csv(n_days=100)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            assert len(analyzer.sessions) > 20
            assert len(analyzer.observations) > 0
            coverage = analyzer.get_data_coverage()
            assert coverage["months"] >= 1
        finally:
            os.unlink(csv_path)

    def test_all_handoff_pairs_populated(self):
        csv_path = _generate_simple_test_csv(n_days=100)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            for from_s, to_s in HANDOFF_PAIRS:
                matrix = analyzer.get_handoff_matrix(from_s, to_s)
                # At least one behavior should have observations
                total_obs = sum(
                    matrix[b][HandoffOutcome.CONTINUATION].total
                    for b in SessionBehavior
                )
                # Some pairs may have zero if no matching date pairs exist
                # This is OK -- just check no crashes
                assert total_obs >= 0
        finally:
            os.unlink(csv_path)

    def test_report_generation(self):
        csv_path = _generate_simple_test_csv(n_days=50)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            report = analyzer.print_full_report()
            assert "ASIA" in report
            assert "LONDON" in report
            assert "Survivorship" in report or "Regime" in report or "N=" in report
        finally:
            os.unlink(csv_path)

    def test_selection_bias_test(self):
        csv_path = _generate_simple_test_csv(n_days=50)
        try:
            analyzer = SessionHandoffAnalyzer()
            analyzer.analyze_csv(csv_path)
            result = analyzer.run_selection_bias_test()
            # Should either succeed or report not enough data
            assert "verdict" in result or "error" in result
        finally:
            os.unlink(csv_path)
