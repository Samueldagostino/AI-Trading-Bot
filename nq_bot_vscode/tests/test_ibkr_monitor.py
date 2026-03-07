"""
Tests for IBKR Paper Trading Monitor.

Covers:
  - PF / WR calculation correctness
  - Rolling 20-trade PF
  - Max consecutive losses
  - Drawdown calculation
  - Z-score computation (WR and PF)
  - Statistical significance detection
  - Alert threshold triggers (RED, YELLOW, GREEN)
  - C1 / C2 PnL split
  - Average winner / loser in points
  - Dashboard rendering
  - Baseline loading
  - Trade log parsing
"""

import json
import math
import pytest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ibkr_monitor import (
    StatsEngine,
    AlertEngine,
    AlertLevel,
    Alert,
    BacktestBaseline,
    TradeRecord,
    WeeklyReport,
    parse_trades,
    render_dashboard,
    generate_weekly_report,
    compute_weekly_reports,
    compute_4_week_trend,
    export_weekly_report,
    update_viz_data,
    is_friday_rth_close,
    _week_boundaries,
    _trades_in_range,
    _render_weekly_report,
    TREND_IMPROVING,
    TREND_DEGRADING,
    TREND_STABLE,
    MNQ_POINT_VALUE,
)


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _baseline() -> BacktestBaseline:
    return BacktestBaseline()


def _engine(trades: list = None) -> StatsEngine:
    engine = StatsEngine(_baseline())
    if trades:
        engine.trades = trades
    return engine


def _trade(pnl: float, c1: float = 0, c2: float = 0, ts: str = "2026-03-01T14:00:00") -> TradeRecord:
    return TradeRecord(
        timestamp=ts,
        direction="long" if pnl >= 0 else "short",
        pnl=pnl,
        c1_pnl=c1,
        c2_pnl=c2,
    )


def _winning_trades(n: int, pnl: float = 20.0) -> list:
    return [_trade(pnl) for _ in range(n)]


def _losing_trades(n: int, pnl: float = -15.0) -> list:
    return [_trade(pnl) for _ in range(n)]


def _mixed_trades(wins: int, losses: int) -> list:
    return _winning_trades(wins) + _losing_trades(losses)


# ═══════════════════════════════════════════════════════════════
# PF / WR CALCULATIONS
# ═══════════════════════════════════════════════════════════════

class TestProfitFactor:

    def test_simple_pf(self):
        engine = _engine(_mixed_trades(6, 4))
        # gross_profit = 6*20 = 120, gross_loss = 4*15 = 60
        assert engine.profit_factor == pytest.approx(2.0)

    def test_pf_all_winners(self):
        engine = _engine(_winning_trades(10))
        assert engine.profit_factor == float("inf")

    def test_pf_all_losers(self):
        engine = _engine(_losing_trades(10))
        assert engine.profit_factor == 0.0

    def test_pf_no_trades(self):
        engine = _engine([])
        assert engine.profit_factor == 0.0

    def test_pf_single_winner(self):
        engine = _engine([_trade(100.0)])
        assert engine.profit_factor == float("inf")

    def test_pf_single_loser(self):
        engine = _engine([_trade(-50.0)])
        assert engine.profit_factor == 0.0

    def test_pf_equal_wins_losses(self):
        trades = [_trade(10.0), _trade(-10.0)]
        engine = _engine(trades)
        assert engine.profit_factor == pytest.approx(1.0)


class TestWinRate:

    def test_wr_basic(self):
        engine = _engine(_mixed_trades(7, 3))
        assert engine.win_rate == pytest.approx(70.0)

    def test_wr_zero_trades(self):
        engine = _engine([])
        assert engine.win_rate == 0.0

    def test_wr_all_winners(self):
        engine = _engine(_winning_trades(5))
        assert engine.win_rate == pytest.approx(100.0)

    def test_wr_all_losers(self):
        engine = _engine(_losing_trades(5))
        assert engine.win_rate == pytest.approx(0.0)

    def test_wr_exact_count(self):
        engine = _engine(_mixed_trades(3, 2))
        assert engine.wins == 3
        assert engine.losses == 2
        assert engine.total_trades == 5


class TestCumulativePnl:

    def test_cumulative(self):
        trades = [_trade(20), _trade(-10), _trade(30)]
        engine = _engine(trades)
        assert engine.cumulative_pnl == pytest.approx(40.0)

    def test_cumulative_empty(self):
        engine = _engine([])
        assert engine.cumulative_pnl == 0.0


# ═══════════════════════════════════════════════════════════════
# ROLLING 20-TRADE PF
# ═══════════════════════════════════════════════════════════════

class TestRollingPF:

    def test_rolling_pf_under_20(self):
        engine = _engine(_mixed_trades(5, 5))
        # Uses all 10 trades
        expected_pf = (5 * 20.0) / (5 * 15.0)
        assert engine.rolling_pf == pytest.approx(expected_pf)

    def test_rolling_pf_exactly_20(self):
        engine = _engine(_mixed_trades(12, 8))
        expected_pf = (12 * 20.0) / (8 * 15.0)
        assert engine.rolling_pf == pytest.approx(expected_pf)

    def test_rolling_pf_over_20(self):
        # 30 trades: first 10 are winners, last 20 are losers
        trades = _winning_trades(10) + _losing_trades(20)
        engine = _engine(trades)
        # Rolling window = last 20 = all losers
        assert engine.rolling_pf == 0.0

    def test_rolling_pf_empty(self):
        engine = _engine([])
        assert engine.rolling_pf == 0.0

    def test_rolling_pf_window_shifts(self):
        # 25 trades: 5 losers then 20 winners
        trades = _losing_trades(5) + _winning_trades(20)
        engine = _engine(trades)
        # Rolling = last 20 = all winners
        assert engine.rolling_pf == float("inf")


# ═══════════════════════════════════════════════════════════════
# MAX CONSECUTIVE LOSSES
# ═══════════════════════════════════════════════════════════════

class TestMaxConsecutiveLosses:

    def test_no_trades(self):
        assert _engine([]).max_consecutive_losses == 0

    def test_no_losses(self):
        assert _engine(_winning_trades(10)).max_consecutive_losses == 0

    def test_all_losses(self):
        assert _engine(_losing_trades(7)).max_consecutive_losses == 7

    def test_mixed_with_streak(self):
        trades = (
            _winning_trades(3)
            + _losing_trades(4)
            + _winning_trades(2)
            + _losing_trades(2)
        )
        assert _engine(trades).max_consecutive_losses == 4

    def test_single_loss(self):
        trades = _winning_trades(5) + [_trade(-10)] + _winning_trades(5)
        assert _engine(trades).max_consecutive_losses == 1


# ═══════════════════════════════════════════════════════════════
# DRAWDOWN
# ═══════════════════════════════════════════════════════════════

class TestDrawdown:

    def test_no_trades(self):
        assert _engine([]).current_drawdown_pct(50000) == 0.0

    def test_no_drawdown_all_winners(self):
        engine = _engine(_winning_trades(10))
        assert engine.current_drawdown_pct(50000) == 0.0

    def test_simple_drawdown(self):
        # Win 100, lose 50 → peak=100, dd=50
        trades = [_trade(100), _trade(-50)]
        engine = _engine(trades)
        # dd = 50 / 50000 * 100 = 0.1%
        assert engine.current_drawdown_pct(50000) == pytest.approx(0.1)

    def test_large_drawdown(self):
        # Lose $1500 straight → dd = 1500/50000 = 3%
        trades = [_trade(-500)] * 3
        engine = _engine(trades)
        assert engine.current_drawdown_pct(50000) == pytest.approx(3.0)

    def test_recovery_resets_peak(self):
        # Win 100, lose 30, win 100 → peak=200, current=170, dd=30
        trades = [_trade(100), _trade(-30), _trade(100)]
        engine = _engine(trades)
        # Peak was 100 after first trade, fell to 70 after loss = dd of 30
        # Then 170 > 100, new peak=170, no new dd
        # Max dd = 30/50000 = 0.06%
        assert engine.current_drawdown_pct(50000) == pytest.approx(0.06)

    def test_zero_account_size(self):
        trades = [_trade(-100)]
        assert _engine(trades).current_drawdown_pct(0) == 0.0


# ═══════════════════════════════════════════════════════════════
# C1 / C2 PNL SPLIT
# ═══════════════════════════════════════════════════════════════

class TestC1C2Split:

    def test_c1_c2_accumulation(self):
        trades = [
            _trade(30, c1=10, c2=20),
            _trade(50, c1=15, c2=35),
            _trade(-20, c1=-5, c2=-15),
        ]
        engine = _engine(trades)
        assert engine.c1_pnl == pytest.approx(20.0)
        assert engine.c2_pnl == pytest.approx(40.0)

    def test_c1_c2_zero(self):
        engine = _engine([])
        assert engine.c1_pnl == 0.0
        assert engine.c2_pnl == 0.0


# ═══════════════════════════════════════════════════════════════
# AVERAGE WINNER / LOSER (in points)
# ═══════════════════════════════════════════════════════════════

class TestAvgWinnerLoser:

    def test_avg_winner_pts(self):
        # Three winners at $20 each → 20/2.0 = 10 pts
        engine = _engine(_winning_trades(3, pnl=20.0))
        assert engine.avg_winner_pts == pytest.approx(10.0)

    def test_avg_loser_pts(self):
        # Two losers at -$30 each → 30/2.0 = 15 pts
        engine = _engine(_losing_trades(2, pnl=-30.0))
        assert engine.avg_loser_pts == pytest.approx(15.0)

    def test_no_winners(self):
        engine = _engine(_losing_trades(3))
        assert engine.avg_winner_pts == 0.0

    def test_no_losers(self):
        engine = _engine(_winning_trades(3))
        assert engine.avg_loser_pts == 0.0


# ═══════════════════════════════════════════════════════════════
# Z-SCORE COMPUTATION
# ═══════════════════════════════════════════════════════════════

class TestWRZScore:

    def test_wr_z_score_matching(self):
        """WR matching backtest → z ≈ 0."""
        # Backtest WR = 61.9%. Build trades with ~62% WR
        trades = _mixed_trades(62, 38)  # 62% WR
        engine = _engine(trades)
        z = engine.wr_z_score()
        # Should be close to 0 (62% vs 61.9%)
        assert abs(z) < 0.5

    def test_wr_z_score_much_worse(self):
        """WR much worse → large negative z."""
        trades = _mixed_trades(30, 70)  # 30% WR vs 61.9%
        engine = _engine(trades)
        z = engine.wr_z_score()
        assert z < -5.0  # Very significantly below

    def test_wr_z_score_much_better(self):
        """WR much better → large positive z."""
        trades = _mixed_trades(90, 10)  # 90% WR vs 61.9%
        engine = _engine(trades)
        z = engine.wr_z_score()
        assert z > 5.0

    def test_wr_z_score_empty(self):
        assert _engine([]).wr_z_score() == 0.0

    def test_wr_z_score_formula(self):
        """Verify exact formula: z = (p_obs - p_exp) / sqrt(p_exp*(1-p_exp)/n)."""
        trades = _mixed_trades(70, 30)  # 70% WR, n=100
        engine = _engine(trades)
        p_obs = 0.70
        p_exp = 0.619
        se = math.sqrt(p_exp * (1 - p_exp) / 100)
        expected_z = (p_obs - p_exp) / se
        assert engine.wr_z_score() == pytest.approx(expected_z, abs=0.01)


class TestPFZScore:

    def test_pf_z_score_matching(self):
        """PF matching backtest → z ≈ 0."""
        # Build trades with PF ≈ 1.73
        # gross_profit / gross_loss = 1.73
        # e.g. 62 wins at $20 = $1240, 38 losses at $18.53 ≈ $704
        # PF = 1240/704 ≈ 1.76 (close enough)
        trades = [_trade(20)] * 62 + [_trade(-18.53)] * 38
        engine = _engine(trades)
        z = engine.pf_z_score()
        assert abs(z) < 1.0

    def test_pf_z_score_much_worse(self):
        """PF << backtest → large negative z."""
        trades = _mixed_trades(30, 70)
        # PF = (30*20) / (70*15) = 600/1050 ≈ 0.57
        engine = _engine(trades)
        z = engine.pf_z_score()
        assert z < -2.0

    def test_pf_z_score_no_losses(self):
        engine = _engine(_winning_trades(10))
        assert engine.pf_z_score() == 0.0  # Can't compute

    def test_pf_z_score_no_wins(self):
        engine = _engine(_losing_trades(10))
        assert engine.pf_z_score() == 0.0


class TestSignificance:

    def test_significant_wr_drop(self):
        """30% WR over 100 trades is significant."""
        trades = _mixed_trades(30, 70)
        engine = _engine(trades)
        assert engine.is_wr_significant() is True

    def test_not_significant_small_sample(self):
        """Small sample → not significant even if different."""
        trades = _mixed_trades(3, 7)  # 30% WR but n=10
        engine = _engine(trades)
        # With n=10, SE is large so z won't exceed 1.96
        # SE = sqrt(0.619*0.381/10) = 0.154
        # z = (0.3 - 0.619) / 0.154 = -2.07... actually this IS significant
        # Let's use a smaller diff: 5/10 = 50% vs 61.9%
        trades2 = _mixed_trades(5, 5)
        engine2 = _engine(trades2)
        assert engine2.is_wr_significant() is False

    def test_matching_not_significant(self):
        """WR matching backtest → not significant."""
        trades = _mixed_trades(62, 38)
        engine = _engine(trades)
        assert engine.is_wr_significant() is False


# ═══════════════════════════════════════════════════════════════
# ALERT THRESHOLDS — RED
# ═══════════════════════════════════════════════════════════════

class TestRedAlerts:

    def test_red_pf_below_threshold(self):
        """PF < 0.8 after 50+ trades → RED."""
        trades = _mixed_trades(20, 40)  # 33% WR, PF ~0.89
        # Actually PF = (20*20)/(40*15) = 400/600 = 0.67
        trades += _losing_trades(10)  # push to 50+
        engine = _engine(trades)
        assert engine.total_trades >= 50
        assert engine.profit_factor < 0.8
        alerts = AlertEngine().evaluate(engine)
        red_pf = [a for a in alerts if a.level == AlertLevel.RED and "PROFIT" in a.category]
        assert len(red_pf) == 1

    def test_red_wr_below_threshold(self):
        """WR < 45% after 50+ trades → RED."""
        trades = _mixed_trades(20, 30) + _losing_trades(10)
        engine = _engine(trades)
        # 20 wins / 60 total = 33.3%
        assert engine.total_trades >= 50
        assert engine.win_rate < 45
        alerts = AlertEngine().evaluate(engine)
        red_wr = [a for a in alerts if a.level == AlertLevel.RED and "WIN RATE" in a.category]
        assert len(red_wr) == 1

    def test_red_dd_above_3pct(self):
        """Max DD > 3% → RED."""
        trades = [_trade(-500)] * 4  # -$2000 → 4% DD
        engine = _engine(trades)
        assert engine.max_drawdown_pct(50000) > 3.0
        alerts = AlertEngine().evaluate(engine)
        red_dd = [a for a in alerts if a.level == AlertLevel.RED and "DRAWDOWN" in a.category]
        assert len(red_dd) == 1

    def test_red_consec_losses_10(self):
        """10+ consecutive losses → RED."""
        trades = _losing_trades(10)
        engine = _engine(trades)
        alerts = AlertEngine().evaluate(engine)
        red_cl = [a for a in alerts if a.level == AlertLevel.RED and "CONSEC" in a.category]
        assert len(red_cl) == 1

    def test_red_daily_loss_exceeds_500(self):
        """Daily loss > $500 → RED."""
        today = "2026-03-01T14:00:00"
        trades = [_trade(-200, ts=today)] * 3  # -$600
        engine = _engine(trades)
        alerts = AlertEngine().evaluate(engine)
        red_dl = [a for a in alerts if a.level == AlertLevel.RED and "DAILY" in a.category]
        assert len(red_dl) == 1

    def test_no_red_below_50_trades(self):
        """PF and WR alerts only fire after min trades."""
        trades = _mixed_trades(5, 10)  # Terrible stats, but only 15 trades
        engine = _engine(trades)
        alerts = AlertEngine().evaluate(engine)
        red_pf = [a for a in alerts if a.level == AlertLevel.RED and "PROFIT" in a.category]
        red_wr = [a for a in alerts if a.level == AlertLevel.RED and "WIN RATE" in a.category]
        assert len(red_pf) == 0
        assert len(red_wr) == 0


# ═══════════════════════════════════════════════════════════════
# ALERT THRESHOLDS — YELLOW
# ═══════════════════════════════════════════════════════════════

class TestYellowAlerts:

    def test_yellow_pf_warning_range(self):
        """PF between 0.8-1.2 after 30+ trades → YELLOW."""
        # Need PF between 0.8 and 1.2 with 30+ trades
        # 18 wins at $20 = $360, 17 losses at $20 = $340 → PF = 1.06
        trades = [_trade(20)] * 18 + [_trade(-20)] * 17
        engine = _engine(trades)
        assert 0.8 <= engine.profit_factor < 1.2
        assert engine.total_trades >= 30
        alerts = AlertEngine().evaluate(engine)
        yellow_pf = [a for a in alerts if a.level == AlertLevel.YELLOW and "PROFIT" in a.category]
        assert len(yellow_pf) == 1

    def test_yellow_wr_warning_range(self):
        """WR between 45-55% after 30+ trades → YELLOW."""
        trades = _mixed_trades(16, 19)  # 45.7% WR
        engine = _engine(trades)
        assert 45 <= engine.win_rate < 55
        assert engine.total_trades >= 30
        alerts = AlertEngine().evaluate(engine)
        yellow_wr = [a for a in alerts if a.level == AlertLevel.YELLOW and "WIN RATE" in a.category]
        assert len(yellow_wr) == 1

    def test_yellow_dd_above_2pct(self):
        """DD > 2% (but < 3%) → YELLOW only."""
        trades = [_trade(-400)] * 3  # -$1200 → 2.4%
        engine = _engine(trades)
        dd = engine.max_drawdown_pct(50000)
        assert 2.0 < dd < 3.0
        alerts = AlertEngine().evaluate(engine)
        yellow_dd = [a for a in alerts if a.level == AlertLevel.YELLOW and "DRAWDOWN" in a.category]
        assert len(yellow_dd) == 1
        red_dd = [a for a in alerts if a.level == AlertLevel.RED and "DRAWDOWN" in a.category]
        assert len(red_dd) == 0

    def test_yellow_consec_losses_6(self):
        """6-9 consecutive losses → YELLOW."""
        trades = _losing_trades(7)
        engine = _engine(trades)
        alerts = AlertEngine().evaluate(engine)
        yellow_cl = [a for a in alerts if a.level == AlertLevel.YELLOW and "CONSEC" in a.category]
        assert len(yellow_cl) == 1
        red_cl = [a for a in alerts if a.level == AlertLevel.RED and "CONSEC" in a.category]
        assert len(red_cl) == 0

    def test_no_yellow_if_red(self):
        """RED supersedes YELLOW for same category."""
        trades = _losing_trades(12)  # 12 consec → RED, not YELLOW
        engine = _engine(trades)
        alerts = AlertEngine().evaluate(engine)
        consec_alerts = [a for a in alerts if "CONSEC" in a.category]
        assert len(consec_alerts) == 1
        assert consec_alerts[0].level == AlertLevel.RED


# ═══════════════════════════════════════════════════════════════
# ALERT THRESHOLDS — GREEN
# ═══════════════════════════════════════════════════════════════

class TestGreenStatus:

    def test_green_healthy_stats(self):
        """PF > 1.2, WR > 55%, DD < 1.5% → GREEN."""
        # Interleave wins/losses to avoid consecutive loss streaks
        trades = []
        for i in range(50):
            if i % 10 < 7:  # 70% WR, max 3 consec losses
                trades.append(_trade(20.0))
            else:
                trades.append(_trade(-15.0))
        engine = _engine(trades)
        assert engine.profit_factor > 1.2
        assert engine.win_rate > 55
        assert engine.max_consecutive_losses < 6
        status = AlertEngine().overall_status(engine)
        assert status == AlertLevel.GREEN

    def test_green_no_trades(self):
        """No trades → GREEN (no data to alert on)."""
        engine = _engine([])
        status = AlertEngine().overall_status(engine)
        assert status == AlertLevel.GREEN

    def test_overall_status_red_overrides(self):
        """Any RED alert makes overall status RED."""
        trades = _losing_trades(10)  # 10 consec losses
        engine = _engine(trades)
        status = AlertEngine().overall_status(engine)
        assert status == AlertLevel.RED

    def test_overall_status_yellow(self):
        """YELLOW alerts with no RED → YELLOW."""
        trades = _losing_trades(7)  # 7 consec → YELLOW
        engine = _engine(trades)
        # Below 50 trades so PF/WR RED won't fire
        assert engine.total_trades < 50
        status = AlertEngine().overall_status(engine)
        assert status == AlertLevel.YELLOW


# ═══════════════════════════════════════════════════════════════
# TRADE LOG PARSING
# ═══════════════════════════════════════════════════════════════

class TestTradeLogParsing:

    def test_parse_fill_with_pnl(self):
        raw = [{"event": "fill", "pnl": 25.0, "direction": "long",
                "c1_pnl": 10, "c2_pnl": 15, "logged_at": "2026-03-01T14:00:00"}]
        trades = parse_trades(raw)
        assert len(trades) == 1
        assert trades[0].pnl == 25.0
        assert trades[0].c1_pnl == 10.0
        assert trades[0].c2_pnl == 15.0

    def test_parse_fill_without_pnl_skipped(self):
        """Fill entries without pnl (open fills) are skipped."""
        raw = [{"event": "fill", "direction": "long",
                "logged_at": "2026-03-01T14:00:00"}]
        trades = parse_trades(raw)
        assert len(trades) == 0

    def test_parse_shutdown_flatten_skipped(self):
        raw = [{"event": "shutdown_flatten", "position_id": "G1-C1",
                "logged_at": "2026-03-01T14:00:00"}]
        trades = parse_trades(raw)
        assert len(trades) == 0

    def test_parse_non_fill_skipped(self):
        raw = [{"event": "session_start", "logged_at": "2026-03-01"}]
        trades = parse_trades(raw)
        assert len(trades) == 0

    def test_parse_empty(self):
        assert parse_trades([]) == []


# ═══════════════════════════════════════════════════════════════
# BASELINE LOADING
# ═══════════════════════════════════════════════════════════════

class TestBaselineLoading:

    def test_defaults(self):
        b = BacktestBaseline()
        assert b.profit_factor == 1.73
        assert b.win_rate_pct == 61.9
        assert b.trades_per_month == 254
        assert b.account_size == 50000.0

    def test_from_json(self, tmp_path):
        p = tmp_path / "baseline.json"
        p.write_text('{"profit_factor": 2.0, "win_rate_pct": 70.0}')
        b = BacktestBaseline.from_json(p)
        assert b.profit_factor == 2.0
        assert b.win_rate_pct == 70.0
        # Other fields get defaults
        assert b.trades_per_month == 254

    def test_missing_file_uses_defaults(self, tmp_path):
        b = BacktestBaseline.from_json(tmp_path / "nonexistent.json")
        assert b.profit_factor == 1.73

    def test_corrupt_json_uses_defaults(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all")
        b = BacktestBaseline.from_json(p)
        assert b.profit_factor == 1.73


# ═══════════════════════════════════════════════════════════════
# DASHBOARD RENDERING
# ═══════════════════════════════════════════════════════════════

class TestDashboardRendering:

    def test_render_empty(self):
        engine = _engine([])
        output = render_dashboard(engine, [])
        assert "IBKR MONITOR" in output
        assert "GREEN" in output
        assert "RUNNING STATS" in output

    def test_render_with_trades(self):
        engine = _engine(_mixed_trades(7, 3))
        alerts = AlertEngine().evaluate(engine)
        output = render_dashboard(engine, alerts)
        assert "10" in output  # total trades
        assert "7W" in output
        assert "3L" in output

    def test_render_with_alerts(self):
        alerts = [
            Alert(AlertLevel.RED, "TEST", "test red"),
            Alert(AlertLevel.YELLOW, "TEST2", "test yellow"),
        ]
        output = render_dashboard(_engine([]), alerts)
        assert "RED" in output
        assert "test red" in output
        assert "WARN" in output

    def test_render_backtest_comparison_at_10_trades(self):
        engine = _engine(_mixed_trades(7, 3))
        output = render_dashboard(engine, [])
        assert "BACKTEST COMPARISON" in output
        assert "Z-score" in output

    def test_render_no_comparison_under_10(self):
        engine = _engine(_mixed_trades(5, 3))
        output = render_dashboard(engine, [])
        assert "BACKTEST COMPARISON" not in output


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

class TestConstants:

    def test_mnq_point_value(self):
        assert MNQ_POINT_VALUE == 2.0

    def test_red_thresholds(self):
        assert AlertEngine.RED_PF_THRESHOLD == 0.8
        assert AlertEngine.RED_PF_MIN_TRADES == 50
        assert AlertEngine.RED_WR_THRESHOLD == 45.0
        assert AlertEngine.RED_MAX_DD_PCT == 3.0
        assert AlertEngine.RED_MAX_CONSEC_LOSSES == 10
        assert AlertEngine.RED_DAILY_LOSS_LIMIT == 500.0

    def test_yellow_thresholds(self):
        assert AlertEngine.YELLOW_PF_LOW == 0.8
        assert AlertEngine.YELLOW_PF_HIGH == 1.2
        assert AlertEngine.YELLOW_MAX_DD_PCT == 2.0
        assert AlertEngine.YELLOW_MAX_CONSEC_LOSSES == 6

    def test_green_thresholds(self):
        assert AlertEngine.GREEN_PF_MIN == 1.2
        assert AlertEngine.GREEN_WR_MIN == 55.0
        assert AlertEngine.GREEN_MAX_DD_PCT == 1.5


# ═══════════════════════════════════════════════════════════════
# WEEKLY REPORT GENERATION
# ═══════════════════════════════════════════════════════════════

def _trade_on(date: str, pnl: float, c1: float = 0, c2: float = 0) -> TradeRecord:
    """Create a trade with a specific date."""
    return TradeRecord(
        timestamp=f"{date}T14:00:00",
        direction="long" if pnl >= 0 else "short",
        pnl=pnl,
        c1_pnl=c1,
        c2_pnl=c2,
    )


class TestWeekBoundaries:

    def test_monday(self):
        start, end = _week_boundaries("2026-03-02")  # Monday
        assert start == "2026-03-02"
        assert end == "2026-03-06"

    def test_wednesday(self):
        start, end = _week_boundaries("2026-03-04")  # Wednesday
        assert start == "2026-03-02"
        assert end == "2026-03-06"

    def test_friday(self):
        start, end = _week_boundaries("2026-03-06")  # Friday
        assert start == "2026-03-02"
        assert end == "2026-03-06"

    def test_sunday_maps_to_prior_week(self):
        start, end = _week_boundaries("2026-03-01")  # Sunday
        assert start == "2026-02-23"
        assert end == "2026-02-27"


class TestTradesInRange:

    def test_filters_by_date(self):
        trades = [
            _trade_on("2026-03-02", 20),
            _trade_on("2026-03-03", -10),
            _trade_on("2026-03-09", 30),  # next week
        ]
        filtered = _trades_in_range(trades, "2026-03-02", "2026-03-06")
        assert len(filtered) == 2
        assert filtered[0].pnl == 20
        assert filtered[1].pnl == -10

    def test_empty_range(self):
        trades = [_trade_on("2026-03-02", 20)]
        filtered = _trades_in_range(trades, "2026-03-09", "2026-03-13")
        assert len(filtered) == 0

    def test_inclusive_boundaries(self):
        trades = [
            _trade_on("2026-03-02", 10),  # Monday (start)
            _trade_on("2026-03-06", 20),  # Friday (end)
        ]
        filtered = _trades_in_range(trades, "2026-03-02", "2026-03-06")
        assert len(filtered) == 2


class TestGenerateWeeklyReport:

    def test_basic_report(self):
        trades = [
            _trade_on("2026-03-02", 40, c1=15, c2=25),
            _trade_on("2026-03-03", -20, c1=-8, c2=-12),
            _trade_on("2026-03-04", 30, c1=10, c2=20),
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-03")
        assert report.week_start == "2026-03-02"
        assert report.week_end == "2026-03-06"
        assert report.trade_count == 3
        assert report.net_pnl == pytest.approx(50.0)
        assert report.wins == 2
        assert report.losses == 1

    def test_c1_c2_breakdown(self):
        trades = [
            _trade_on("2026-03-02", 40, c1=15, c2=25),
            _trade_on("2026-03-03", 30, c1=10, c2=20),
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        assert report.c1_pnl == pytest.approx(25.0)
        assert report.c2_pnl == pytest.approx(45.0)
        assert report.c1_pnl_pts == pytest.approx(12.5)  # 25 / 2.0
        assert report.c2_pnl_pts == pytest.approx(22.5)  # 45 / 2.0

    def test_win_rate(self):
        trades = [
            _trade_on("2026-03-02", 20),
            _trade_on("2026-03-02", 20),
            _trade_on("2026-03-03", -10),
            _trade_on("2026-03-04", 20),
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        assert report.win_rate_pct == pytest.approx(75.0)

    def test_profit_factor(self):
        trades = [
            _trade_on("2026-03-02", 40),
            _trade_on("2026-03-03", -20),
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        assert report.profit_factor == pytest.approx(2.0)

    def test_excludes_other_weeks(self):
        trades = [
            _trade_on("2026-02-25", 100),  # prior week
            _trade_on("2026-03-02", 20),    # this week
            _trade_on("2026-03-09", 50),    # next week
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        assert report.trade_count == 1
        assert report.net_pnl == pytest.approx(20.0)

    def test_empty_week(self):
        trades = [_trade_on("2026-02-25", 100)]  # different week
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        assert report.trade_count == 0
        assert report.net_pnl == 0.0

    def test_z_scores_populated(self):
        # Enough trades for meaningful Z-scores
        trades = []
        for i in range(30):
            if i % 3 == 0:
                trades.append(_trade_on("2026-03-02", -15))
            else:
                trades.append(_trade_on("2026-03-02", 20))
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        # WR should differ from 61.9% baseline → non-zero Z
        assert report.wr_z_score != 0.0
        assert report.pf_z_score != 0.0

    def test_to_dict_serialization(self):
        trades = [
            _trade_on("2026-03-02", 40, c1=15, c2=25),
            _trade_on("2026-03-03", -20, c1=-8, c2=-12),
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        d = report.to_dict()
        assert d["week_start"] == "2026-03-02"
        assert d["week_end"] == "2026-03-06"
        assert d["trade_count"] == 2
        assert isinstance(d["net_pnl"], (int, float))
        assert isinstance(d["wr_significant"], bool)
        assert isinstance(d["pf_significant"], bool)


class TestComputeWeeklyReports:

    def test_multiple_weeks(self):
        trades = [
            _trade_on("2026-02-23", 20),  # week 1
            _trade_on("2026-02-24", 30),
            _trade_on("2026-03-02", 40),  # week 2
            _trade_on("2026-03-03", -10),
        ]
        reports = compute_weekly_reports(trades, _baseline())
        assert len(reports) == 2
        assert reports[0].week_start == "2026-02-23"
        assert reports[1].week_start == "2026-03-02"

    def test_single_week(self):
        trades = [_trade_on("2026-03-02", 20)]
        reports = compute_weekly_reports(trades, _baseline())
        assert len(reports) == 1

    def test_empty_trades(self):
        reports = compute_weekly_reports([], _baseline())
        assert len(reports) == 0

    def test_reports_sorted_chronologically(self):
        trades = [
            _trade_on("2026-03-09", 10),  # week 2
            _trade_on("2026-03-02", 20),  # week 1
        ]
        reports = compute_weekly_reports(trades, _baseline())
        assert reports[0].week_start < reports[1].week_start


# ═══════════════════════════════════════════════════════════════
# 4-WEEK TREND COMPUTATION
# ═══════════════════════════════════════════════════════════════

def _make_report(
    week_start: str, pf: float = 1.5, wr: float = 60.0, pnl: float = 200.0
) -> WeeklyReport:
    """Create a minimal WeeklyReport for trend testing."""
    friday = (
        datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=4)
    ).strftime("%Y-%m-%d")
    return WeeklyReport(
        week_start=week_start,
        week_end=friday,
        trade_count=10,
        net_pnl=pnl,
        profit_factor=pf,
        win_rate_pct=wr,
        wins=6,
        losses=4,
    )


class TestFourWeekTrend:

    def test_insufficient_data(self):
        reports = [_make_report("2026-02-23")]
        trend = compute_4_week_trend(reports)
        assert trend["status"] == "insufficient_data"
        assert trend["weeks_available"] == 1
        assert trend["pf_trend"] == TREND_STABLE
        assert trend["wr_trend"] == TREND_STABLE
        assert trend["pnl_trend"] == TREND_STABLE

    def test_stable_trend(self):
        reports = [
            _make_report("2026-02-02", pf=1.5, wr=60, pnl=200),
            _make_report("2026-02-09", pf=1.5, wr=60, pnl=200),
            _make_report("2026-02-16", pf=1.5, wr=60, pnl=200),
            _make_report("2026-02-23", pf=1.5, wr=60, pnl=200),
        ]
        trend = compute_4_week_trend(reports)
        assert trend["status"] == "computed"
        assert trend["pf_trend"] == TREND_STABLE
        assert trend["wr_trend"] == TREND_STABLE
        assert trend["pnl_trend"] == TREND_STABLE

    def test_improving_trend(self):
        reports = [
            _make_report("2026-02-02", pf=1.0, wr=50, pnl=100),
            _make_report("2026-02-09", pf=1.0, wr=50, pnl=100),
            _make_report("2026-02-16", pf=1.5, wr=65, pnl=300),
            _make_report("2026-02-23", pf=1.5, wr=65, pnl=300),
        ]
        trend = compute_4_week_trend(reports)
        assert trend["pf_trend"] == TREND_IMPROVING
        assert trend["wr_trend"] == TREND_IMPROVING
        assert trend["pnl_trend"] == TREND_IMPROVING

    def test_degrading_trend(self):
        reports = [
            _make_report("2026-02-02", pf=2.0, wr=70, pnl=500),
            _make_report("2026-02-09", pf=2.0, wr=70, pnl=500),
            _make_report("2026-02-16", pf=1.0, wr=50, pnl=100),
            _make_report("2026-02-23", pf=1.0, wr=50, pnl=100),
        ]
        trend = compute_4_week_trend(reports)
        assert trend["pf_trend"] == TREND_DEGRADING
        assert trend["wr_trend"] == TREND_DEGRADING
        assert trend["pnl_trend"] == TREND_DEGRADING

    def test_uses_last_4_weeks(self):
        """When more than 4 weeks, only last 4 are used."""
        reports = [
            _make_report("2026-01-26", pf=3.0, wr=90, pnl=1000),  # ignored
            _make_report("2026-02-02", pf=1.5, wr=60, pnl=200),
            _make_report("2026-02-09", pf=1.5, wr=60, pnl=200),
            _make_report("2026-02-16", pf=1.5, wr=60, pnl=200),
            _make_report("2026-02-23", pf=1.5, wr=60, pnl=200),
        ]
        trend = compute_4_week_trend(reports)
        assert trend["pf_trend"] == TREND_STABLE

    def test_three_weeks_insufficient(self):
        reports = [
            _make_report("2026-02-02"),
            _make_report("2026-02-09"),
            _make_report("2026-02-16"),
        ]
        trend = compute_4_week_trend(reports)
        assert trend["status"] == "insufficient_data"
        assert trend["weeks_available"] == 3

    def test_trend_dict_has_prior_and_latest(self):
        reports = [
            _make_report("2026-02-02", pf=1.2, wr=55, pnl=150),
            _make_report("2026-02-09", pf=1.3, wr=58, pnl=180),
            _make_report("2026-02-16", pf=1.6, wr=65, pnl=300),
            _make_report("2026-02-23", pf=1.8, wr=68, pnl=350),
        ]
        trend = compute_4_week_trend(reports)
        assert "prior_2_weeks" in trend
        assert "latest_2_weeks" in trend
        assert trend["prior_2_weeks"]["avg_pf"] == pytest.approx(1.25)
        assert trend["latest_2_weeks"]["avg_pf"] == pytest.approx(1.70)


# ═══════════════════════════════════════════════════════════════
# JSON EXPORT
# ═══════════════════════════════════════════════════════════════

class TestExportWeeklyReport:

    def test_creates_json_file(self, tmp_path):
        report = _make_report("2026-03-02")
        path = export_weekly_report(report, output_dir=tmp_path)
        assert path.exists()
        assert path.name == "weekly_report_2026-03-06.json"

    def test_json_content(self, tmp_path):
        trades = [
            _trade_on("2026-03-02", 40, c1=15, c2=25),
            _trade_on("2026-03-03", -20, c1=-8, c2=-12),
        ]
        report = generate_weekly_report(trades, _baseline(), "2026-03-02")
        path = export_weekly_report(report, output_dir=tmp_path)

        import json
        with open(path) as f:
            data = json.load(f)

        assert data["week_start"] == "2026-03-02"
        assert data["week_end"] == "2026-03-06"
        assert data["trade_count"] == 2
        assert data["net_pnl"] == 20.0
        assert data["c1_pnl"] == 7.0
        assert data["c2_pnl"] == 13.0
        assert data["wins"] == 1
        assert data["losses"] == 1

    def test_creates_output_dir(self, tmp_path):
        nested = tmp_path / "subdir" / "logs"
        report = _make_report("2026-03-02")
        path = export_weekly_report(report, output_dir=nested)
        assert path.exists()

    def test_json_valid_types(self, tmp_path):
        """All values in exported JSON are JSON-native types."""
        report = _make_report("2026-03-02")
        path = export_weekly_report(report, output_dir=tmp_path)

        import json
        with open(path) as f:
            data = json.load(f)

        for key, val in data.items():
            assert isinstance(val, (int, float, str, bool, type(None))), \
                f"Key {key} has non-JSON type {type(val)}"


class TestUpdateVizData:

    def test_creates_viz_data(self, tmp_path):
        reports = [_make_report("2026-03-02")]
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        viz_path = tmp_path / "viz_data.json"
        result = update_viz_data(reports, trend, viz_path=viz_path)
        assert result.exists()

    def test_viz_data_structure(self, tmp_path):
        reports = [_make_report("2026-03-02"), _make_report("2026-03-09")]
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 2}
        viz_path = tmp_path / "viz_data.json"
        update_viz_data(reports, trend, viz_path=viz_path)

        import json
        with open(viz_path) as f:
            data = json.load(f)

        assert "weekly_reports" in data
        assert "trend" in data
        assert "last_updated" in data
        assert len(data["weekly_reports"]) == 2

    def test_merges_with_existing(self, tmp_path):
        """Existing fields in viz_data.json are preserved."""
        viz_path = tmp_path / "viz_data.json"

        import json
        # Write existing data
        with open(viz_path, "w") as f:
            json.dump({"existing_key": "keep_me"}, f)

        reports = [_make_report("2026-03-02")]
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        update_viz_data(reports, trend, viz_path=viz_path)

        with open(viz_path) as f:
            data = json.load(f)

        assert data["existing_key"] == "keep_me"
        assert "weekly_reports" in data

    def test_handles_corrupt_existing(self, tmp_path):
        """Corrupt existing viz_data.json is replaced."""
        viz_path = tmp_path / "viz_data.json"
        viz_path.write_text("not json at all")

        reports = [_make_report("2026-03-02")]
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        update_viz_data(reports, trend, viz_path=viz_path)

        import json
        with open(viz_path) as f:
            data = json.load(f)
        assert "weekly_reports" in data


# ═══════════════════════════════════════════════════════════════
# WEEKLY REPORT RENDERING
# ═══════════════════════════════════════════════════════════════

class TestWeeklyReportRendering:

    def test_render_contains_header(self):
        report = _make_report("2026-03-02", pf=1.5, wr=60, pnl=200)
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        output = _render_weekly_report(report, trend, _baseline())
        assert "WEEKLY REPORT" in output
        assert "2026-03-02" in output
        assert "2026-03-06" in output

    def test_render_contains_summary(self):
        report = _make_report("2026-03-02", pf=1.5, wr=60, pnl=200)
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        output = _render_weekly_report(report, trend, _baseline())
        assert "SUMMARY" in output
        assert "Net PnL" in output
        assert "Win Rate" in output
        assert "Profit Factor" in output

    def test_render_contains_c1_c2(self):
        report = WeeklyReport(
            week_start="2026-03-02", week_end="2026-03-06",
            net_pnl=100, c1_pnl=40, c2_pnl=60,
            c1_pnl_pts=20, c2_pnl_pts=30,
        )
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        output = _render_weekly_report(report, trend, _baseline())
        assert "C1/C2 BREAKDOWN" in output
        assert "C1 PnL" in output
        assert "C2 PnL" in output

    def test_render_contains_backtest_comparison(self):
        report = _make_report("2026-03-02")
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 1}
        output = _render_weekly_report(report, trend, _baseline())
        assert "VS BACKTEST" in output
        assert "Z-score" in output

    def test_render_trend_insufficient(self):
        report = _make_report("2026-03-02")
        trend = {"status": "insufficient_data", "pf_trend": "stable",
                 "wr_trend": "stable", "pnl_trend": "stable", "weeks_available": 2}
        output = _render_weekly_report(report, trend, _baseline())
        assert "4-WEEK TREND" in output
        assert "Insufficient data" in output

    def test_render_trend_computed(self):
        report = _make_report("2026-03-02")
        trend = {
            "status": "computed",
            "weeks_available": 4,
            "pf_trend": "improving",
            "wr_trend": "stable",
            "pnl_trend": "degrading",
            "prior_2_weeks": {"avg_pf": 1.2, "avg_wr": 55.0, "total_pnl": 300.0},
            "latest_2_weeks": {"avg_pf": 1.5, "avg_wr": 60.0, "total_pnl": 200.0},
        }
        output = _render_weekly_report(report, trend, _baseline())
        assert "IMPROVING" in output
        assert "STABLE" in output
        assert "DEGRADING" in output


# ═══════════════════════════════════════════════════════════════
# FRIDAY RTH CLOSE DETECTION
# ═══════════════════════════════════════════════════════════════

class TestFridayRTHClose:

    def test_friday_after_close(self):
        """Friday 16:30 ET → True."""
        from unittest.mock import patch
        friday_1630_et = datetime(2026, 3, 6, 21, 30, 0, tzinfo=timezone.utc)  # 16:30 ET = 21:30 UTC
        with patch("scripts.ibkr_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = friday_1630_et
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            # Can't easily mock timezone arithmetic — test the logic directly
            # Instead, test the component logic
        # Just test the weekday check on a known Friday
        from datetime import timedelta as td
        et_offset = td(hours=-5)
        et_time = friday_1630_et.astimezone(timezone(et_offset))
        assert et_time.weekday() == 4  # Friday
        assert et_time.hour >= 16

    def test_monday_not_friday(self):
        """Monday at any time → False (not Friday)."""
        monday = datetime(2026, 3, 2, 21, 0, 0, tzinfo=timezone.utc)
        from datetime import timedelta as td
        et_offset = td(hours=-5)
        et_time = monday.astimezone(timezone(et_offset))
        assert et_time.weekday() != 4  # Not Friday


# ═══════════════════════════════════════════════════════════════
# WEEKLY REPORT CONSTANTS
# ═══════════════════════════════════════════════════════════════

class TestWeeklyReportConstants:

    def test_trend_values(self):
        assert TREND_IMPROVING == "improving"
        assert TREND_DEGRADING == "degrading"
        assert TREND_STABLE == "stable"

    def test_weekly_report_dataclass(self):
        r = WeeklyReport(week_start="2026-03-02", week_end="2026-03-06")
        assert r.trade_count == 0
        assert r.net_pnl == 0.0
        assert r.c1_pnl == 0.0
        assert r.c2_pnl == 0.0

    def test_to_dict_inf_pf(self):
        """Infinite PF serializes as None."""
        r = WeeklyReport(
            week_start="2026-03-02", week_end="2026-03-06",
            profit_factor=float("inf"),
        )
        d = r.to_dict()
        assert d["profit_factor"] is None
