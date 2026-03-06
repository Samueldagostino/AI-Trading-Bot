"""
Tests for QuantData Integration
==================================
14 tests covering MarketContext, NQContextTranslator, QuantDataClient,
ContextScorer, paper journal integration, and correlation analyzer.

All tests are offline — no network calls required.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project is on path
_TESTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _TESTS_DIR.parent
_BOT_DIR = _ROOT_DIR / "nq_bot_vscode"
sys.path.insert(0, str(_BOT_DIR))
sys.path.insert(0, str(_ROOT_DIR / "scripts"))

from data_feeds.market_context import MarketContext, NQContextTranslator
from data_feeds.quantdata_client import QuantDataClient
from data_feeds.context_scoring import ContextScorer


class TestMarketContextCreation(unittest.TestCase):
    """test_market_context_creation — all fields populated correctly."""

    def test_all_fields_populated(self):
        ctx = MarketContext(
            timestamp=datetime(2026, 3, 6, 14, 30),
            gamma_regime="negative",
            total_gex=-500.0,
            gamma_flip_level=603.0,
            nearest_wall_above=610.0,
            nearest_wall_below=595.0,
            flow_direction="bullish",
            net_premium=200_000_000.0,
            call_premium=1_700_000_000.0,
            put_premium=1_500_000_000.0,
            dark_bias="bullish",
            dark_pool_levels=[675.50, 672.00],
            skew_regime="elevated",
            skew_slope=0.18,
            source="api",
            age_seconds=30.0,
        )
        self.assertEqual(ctx.gamma_regime, "negative")
        self.assertEqual(ctx.total_gex, -500.0)
        self.assertEqual(ctx.gamma_flip_level, 603.0)
        self.assertEqual(ctx.nearest_wall_above, 610.0)
        self.assertEqual(ctx.nearest_wall_below, 595.0)
        self.assertEqual(ctx.flow_direction, "bullish")
        self.assertEqual(ctx.net_premium, 200_000_000.0)
        self.assertEqual(ctx.dark_bias, "bullish")
        self.assertEqual(ctx.dark_pool_levels, [675.50, 672.00])
        self.assertEqual(ctx.skew_regime, "elevated")
        self.assertEqual(ctx.skew_slope, 0.18)
        self.assertEqual(ctx.source, "api")
        self.assertEqual(ctx.age_seconds, 30.0)


class TestMarketContextFrozen(unittest.TestCase):
    """test_market_context_frozen — immutable after creation."""

    def test_frozen_immutable(self):
        ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="neutral",
            total_gex=0.0,
            gamma_flip_level=0.0,
        )
        with self.assertRaises(AttributeError):
            ctx.gamma_regime = "negative"  # type: ignore


class TestContextFavorableNegativeGamma(unittest.TestCase):
    """test_context_favorable_negative_gamma — negative gamma returns True."""

    def test_negative_gamma_favorable(self):
        ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="negative",
            total_gex=-500.0,
            gamma_flip_level=603.0,
        )
        self.assertTrue(ctx.is_favorable_for_momentum())


class TestContextUnfavorablePositiveGamma(unittest.TestCase):
    """test_context_unfavorable_positive_gamma — positive gamma returns False."""

    def test_positive_gamma_unfavorable(self):
        ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="positive",
            total_gex=500.0,
            gamma_flip_level=603.0,
        )
        self.assertFalse(ctx.is_favorable_for_momentum())


class TestFlowAlignsWithLong(unittest.TestCase):
    """test_flow_aligns_with_long — bullish flow + long = True."""

    def test_bullish_flow_long(self):
        ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="neutral",
            total_gex=0.0,
            gamma_flip_level=0.0,
            flow_direction="bullish",
        )
        self.assertTrue(ctx.aligns_with_direction("long"))


class TestFlowMisalignsWithLong(unittest.TestCase):
    """test_flow_misaligns_with_long — bearish flow + long = False."""

    def test_bearish_flow_long(self):
        ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="neutral",
            total_gex=0.0,
            gamma_flip_level=0.0,
            flow_direction="bearish",
        )
        self.assertFalse(ctx.aligns_with_direction("long"))


class TestQQQToNQTranslation(unittest.TestCase):
    """test_qqq_to_nq_translation — QQQ $500 → NQ ~20,000."""

    def test_price_translation(self):
        translator = NQContextTranslator()
        nq_price = translator._qqq_to_nq_price(500.0)
        self.assertEqual(nq_price, 20000.0)

    def test_none_price(self):
        translator = NQContextTranslator()
        self.assertIsNone(translator._qqq_to_nq_price(None))

    def test_full_translation(self):
        translator = NQContextTranslator()
        qqq_ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="negative",
            total_gex=-500.0,
            gamma_flip_level=500.0,
            nearest_wall_above=510.0,
            nearest_wall_below=490.0,
        )
        nq_ctx = translator.translate(qqq_ctx)
        self.assertEqual(nq_ctx.gamma_regime, "negative")
        self.assertEqual(nq_ctx.gamma_flip_level, 20000.0)
        self.assertEqual(nq_ctx.nearest_wall_above, 20400.0)
        self.assertEqual(nq_ctx.nearest_wall_below, 19600.0)


class TestSPYQQQConflictUsesQQQ(unittest.TestCase):
    """test_spy_qqq_conflict_uses_qqq — QQQ takes priority."""

    def test_qqq_priority(self):
        translator = NQContextTranslator()
        qqq_ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="negative",
            total_gex=-500.0,
            gamma_flip_level=500.0,
        )
        spy_ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="positive",
            total_gex=500.0,
            gamma_flip_level=600.0,
        )
        nq_ctx = translator.translate(qqq_ctx, spy_ctx)
        # QQQ takes priority
        self.assertEqual(nq_ctx.gamma_regime, "negative")


class TestManualSnapshotLoading(unittest.TestCase):
    """test_manual_snapshot_loading — reads from JSON file."""

    def test_load_manual(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manual_path = Path(tmpdir) / "quantdata_manual_input.json"
            manual_path.write_text(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "symbol": "SPY",
                "gex": {
                    "total_gex_millions": -500,
                    "gamma_regime": "negative",
                    "gamma_flip_level": 603.0,
                },
                "net_flow": {
                    "call_premium_billions": 1.7,
                    "put_premium_billions": 1.5,
                    "flow_direction": "bullish",
                },
                "dark_pool": {"dark_bias": "neutral", "significant_levels": []},
                "vol_skew": {"skew_regime": "normal"},
            }))

            client = QuantDataClient()
            client.MANUAL_INPUT_PATH = manual_path
            data = client.load_manual_snapshot()

            self.assertEqual(data["gamma_regime"], "negative")
            self.assertEqual(data["flow_direction"], "bullish")
            self.assertEqual(data["source"], "manual")


class TestStaleSnapshotWarning(unittest.TestCase):
    """test_stale_snapshot_warning — >2hr old triggers warning."""

    def test_stale_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manual_path = Path(tmpdir) / "quantdata_manual_input.json"
            old_time = (datetime.now() - timedelta(hours=3)).isoformat()
            manual_path.write_text(json.dumps({
                "timestamp": old_time,
                "symbol": "SPY",
                "gex": {"gamma_regime": "neutral"},
                "net_flow": {"flow_direction": "neutral"},
                "dark_pool": {"dark_bias": "neutral"},
                "vol_skew": {"skew_regime": "normal"},
            }))

            client = QuantDataClient()
            client.MANUAL_INPUT_PATH = manual_path

            with self.assertLogs("data_feeds.quantdata_client", level="WARNING") as cm:
                data = client.load_manual_snapshot()

            self.assertTrue(any("stale" in msg for msg in cm.output))
            self.assertGreater(data["age_seconds"], 7200)


class TestDefaultNeutralContext(unittest.TestCase):
    """test_default_neutral_context — missing file returns neutral defaults."""

    def test_missing_file_defaults(self):
        client = QuantDataClient()
        client.MANUAL_INPUT_PATH = Path("/nonexistent/path.json")
        data = client.load_manual_snapshot()

        self.assertEqual(data["gamma_regime"], "neutral")
        self.assertEqual(data["flow_direction"], "neutral")
        self.assertEqual(data["dark_bias"], "neutral")
        self.assertEqual(data["skew_regime"], "normal")
        self.assertEqual(data["source"], "default")


class TestContextScorerDisabled(unittest.TestCase):
    """test_context_scorer_disabled — returns 0.0 when ENABLED=False."""

    def test_disabled_returns_zero(self):
        scorer = ContextScorer()
        self.assertFalse(scorer.ENABLED)

        ctx = MarketContext(
            timestamp=datetime.now(),
            gamma_regime="negative",
            total_gex=-500.0,
            gamma_flip_level=603.0,
            flow_direction="bullish",
        )
        adjustment = scorer.score_adjustment(ctx, "long")
        self.assertEqual(adjustment, 0.0)

    def test_none_context_returns_zero(self):
        scorer = ContextScorer()
        self.assertEqual(scorer.score_adjustment(None, "long"), 0.0)


class TestJournalIncludesContext(unittest.TestCase):
    """test_journal_includes_context — trade record has market_context field."""

    def test_trade_record_has_context_fields(self):
        from paper_trading_journal import TradeRecord
        ctx_dict = {
            "gamma_regime": "negative",
            "flow_direction": "bullish",
        }
        record = TradeRecord(
            trade_id=1,
            direction="long",
            total_pnl=25.0,
            market_context=ctx_dict,
            gamma_regime_at_entry="negative",
            flow_aligned_with_trade=True,
            favorable_for_momentum=True,
        )
        from dataclasses import asdict
        d = asdict(record)
        self.assertEqual(d["market_context"]["gamma_regime"], "negative")
        self.assertEqual(d["gamma_regime_at_entry"], "negative")
        self.assertTrue(d["flow_aligned_with_trade"])
        self.assertTrue(d["favorable_for_momentum"])


class TestCorrelationAnalyzerOutput(unittest.TestCase):
    """test_correlation_analyzer_output — mock trades, verify grouping."""

    def test_analyze_gamma_regime_grouping(self):
        sys.path.insert(0, str(_ROOT_DIR / "scripts"))
        from analyze_quantdata_correlation import (
            analyze_gamma_regime,
            analyze_flow_alignment,
            compute_metrics,
        )

        # Create mock trades
        trades = []
        for i in range(30):
            trades.append({
                "total_pnl": 20.0 if i % 2 == 0 else -10.0,
                "market_context": {"gamma_regime": "negative"},
                "flow_aligned_with_trade": True,
            })
        for i in range(30):
            trades.append({
                "total_pnl": 10.0 if i % 3 == 0 else -15.0,
                "market_context": {"gamma_regime": "positive"},
                "flow_aligned_with_trade": False,
            })

        # Test gamma regime grouping
        gamma = analyze_gamma_regime(trades)
        self.assertIn("negative", gamma)
        self.assertIn("positive", gamma)
        self.assertEqual(gamma["negative"]["count"], 30)
        self.assertEqual(gamma["positive"]["count"], 30)

        # Test flow alignment grouping
        flow = analyze_flow_alignment(trades)
        self.assertEqual(flow["aligned"]["count"], 30)
        self.assertEqual(flow["misaligned"]["count"], 30)


if __name__ == "__main__":
    unittest.main()
