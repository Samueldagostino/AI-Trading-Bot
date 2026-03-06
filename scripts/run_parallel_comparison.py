#!/usr/bin/env python3
"""
Paper-to-Live Parallel Comparison Runner
==========================================
Runs two TradingOrchestrator instances on the same market data feed
and compares every decision for divergence detection.

Usage:
    # Paper vs paper (testing/validation)
    python scripts/run_parallel_comparison.py --mode paper-paper

    # Paper vs live (pre-scaling safety net)
    python scripts/run_parallel_comparison.py --mode paper-live

    # With custom log output
    python scripts/run_parallel_comparison.py --mode paper-paper \
        --log logs/comparison_log.json --bars 50
"""

import argparse
import asyncio
import json
import logging
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "nq_bot_vscode"))

from nq_bot_vscode.config.settings import BotConfig, CONFIG
from nq_bot_vscode.features.engine import Bar
from nq_bot_vscode.monitoring.divergence_tracker import (
    ComparisonResult,
    DivergenceTracker,
    FillComparison,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("parallel_comparison")


class ParallelComparisonRunner:
    """
    Runs TWO orchestrator instances on the SAME market data feed.

    Instance A: Paper mode (no real orders)
    Instance B: Paper mode (for testing) or Live mode (real IBKR orders)

    Both instances receive identical bars at the same timestamps.
    All decisions are compared and divergences are tracked.
    """

    def __init__(
        self,
        config: BotConfig = CONFIG,
        mode: str = "paper-paper",
        log_path: Optional[str] = None,
    ):
        self.config = config
        self.mode = mode
        self._log_path = log_path or "logs/comparison_log.json"
        self._tracker = DivergenceTracker(log_path=log_path)
        self._paper_bot = None
        self._live_bot = None
        self._bar_index = 0
        self._paper_trades: List[dict] = []
        self._live_trades: List[dict] = []

    async def initialize(self) -> None:
        """Initialize both orchestrator instances with identical config."""
        from nq_bot_vscode.main import TradingOrchestrator

        # Both instances get identical config (deep copy to avoid shared state)
        config_a = deepcopy(self.config)
        config_b = deepcopy(self.config)

        # Force paper mode on both for paper-paper
        config_a.execution.paper_trading = True
        if self.mode == "paper-paper":
            config_b.execution.paper_trading = True
        else:
            config_b.execution.paper_trading = False

        self._paper_bot = TradingOrchestrator(config_a)
        self._live_bot = TradingOrchestrator(config_b)

        await self._paper_bot.initialize(skip_db=True)
        await self._live_bot.initialize(skip_db=True)

        logger.info(
            "Parallel comparison initialized: mode=%s, paper=%s, live=%s",
            self.mode, "TradingOrchestrator", "TradingOrchestrator",
        )

    async def process_bar(self, bar: Bar) -> ComparisonResult:
        """
        Feed a bar to both instances and compare decisions.

        Args:
            bar: Market data bar to process

        Returns:
            ComparisonResult with any divergences flagged
        """
        self._bar_index += 1

        # Feed identical bar to both instances
        paper_result = await self._paper_bot.process_bar(bar)
        live_result = await self._live_bot.process_bar(bar)

        # Track trades
        if paper_result and paper_result.get("action") == "entry":
            self._paper_trades.append(paper_result)
        if live_result and live_result.get("action") == "entry":
            self._live_trades.append(live_result)

        # Compare decisions
        comparison = self._tracker.compare_decisions(
            bar_timestamp=bar.timestamp,
            bar_index=self._bar_index,
            paper_result=paper_result,
            live_result=live_result,
        )

        # Compare fills for matched entries
        if paper_result and live_result:
            if (paper_result.get("action") == "entry" and
                    live_result.get("action") == "entry"):
                self._tracker.compare_fills(
                    trade_id=f"bar_{self._bar_index}",
                    direction=paper_result.get("direction", ""),
                    paper_entry_price=paper_result.get("entry_price", 0.0),
                    live_entry_price=live_result.get("entry_price", 0.0),
                    paper_fill_time=bar.timestamp,
                    live_fill_time=bar.timestamp,
                    paper_stop=paper_result.get("stop", 0.0),
                    live_stop=live_result.get("stop", 0.0),
                )

        if not comparison.is_clean:
            for div in comparison.divergences:
                logger.warning(
                    "Bar %d: %s — %s",
                    self._bar_index, div.category.value, div.description,
                )

        return comparison

    async def process_htf_bar(self, timeframe: str, bar) -> None:
        """Feed an HTF bar to both instances."""
        self._paper_bot.process_htf_bar(timeframe, bar)
        self._live_bot.process_htf_bar(timeframe, bar)

    def get_summary(self) -> dict:
        """Get current comparison summary."""
        return self._tracker.get_summary()

    def get_verdict(self) -> dict:
        """Get pass/fail verdict."""
        return self._tracker.get_verdict()

    def save_log(self) -> None:
        """Save comparison log to disk."""
        self._tracker.save_log(self._log_path)

    @property
    def tracker(self) -> DivergenceTracker:
        return self._tracker

    @property
    def bars_processed(self) -> int:
        return self._bar_index


async def run_sample_comparison(n_bars: int = 10, log_path: str = "logs/comparison_log.json"):
    """
    Run a sample comparison with synthetic bars for validation.

    Generates deterministic bars and feeds them to both instances.
    Since both receive identical data, divergence should be 0%.
    """
    import numpy as np
    np.random.seed(42)

    runner = ParallelComparisonRunner(
        config=CONFIG,
        mode="paper-paper",
        log_path=log_path,
    )
    await runner.initialize()

    base_price = 21000.0
    logger.info("Running sample comparison: %d bars...", n_bars)

    for i in range(n_bars):
        # Generate a deterministic bar
        noise = np.random.randn() * 5
        bar = Bar(
            timestamp=datetime(2025, 10, 15, 9, 30 + i * 2, tzinfo=timezone.utc),
            open=round(base_price + noise, 2),
            high=round(base_price + noise + abs(np.random.randn()) * 3, 2),
            low=round(base_price + noise - abs(np.random.randn()) * 3, 2),
            close=round(base_price + noise + np.random.randn() * 2, 2),
            volume=int(1000 + np.random.randint(0, 500)),
            bid_volume=int(400 + np.random.randint(0, 200)),
            ask_volume=int(400 + np.random.randint(0, 200)),
            delta=int(np.random.randint(-100, 100)),
        )
        comparison = await runner.process_bar(bar)

    # Print summary
    summary = runner.get_summary()
    verdict = runner.get_verdict()

    logger.info("=" * 60)
    logger.info("COMPARISON SUMMARY")
    logger.info("=" * 60)
    logger.info("  Bars compared:    %d", summary["bars_compared"])
    logger.info("  Agreement rate:   %.1f%%", summary["agreement_rate"])
    logger.info("  Total divergences: %d", summary["total_divergences"])
    if summary["by_category"]:
        for cat, count in summary["by_category"].items():
            logger.info("    %s: %d", cat, count)
    logger.info("  Verdict:          %s", verdict["verdict"])
    if verdict["fail_reasons"]:
        for reason in verdict["fail_reasons"]:
            logger.info("    FAIL: %s", reason)
    logger.info("=" * 60)

    runner.save_log()
    return summary, verdict


def main():
    parser = argparse.ArgumentParser(
        description="Paper-to-live parallel comparison runner"
    )
    parser.add_argument(
        "--mode", default="paper-paper",
        choices=["paper-paper", "paper-live"],
        help="Comparison mode (default: paper-paper)",
    )
    parser.add_argument(
        "--log", default="logs/comparison_log.json",
        help="Output log path (default: logs/comparison_log.json)",
    )
    parser.add_argument(
        "--bars", type=int, default=10,
        help="Number of sample bars for paper-paper mode (default: 10)",
    )
    args = parser.parse_args()

    if args.mode == "paper-paper":
        summary, verdict = asyncio.run(
            run_sample_comparison(n_bars=args.bars, log_path=args.log)
        )
        sys.exit(0 if verdict["verdict"] == "PASS" else 1)
    else:
        logger.info("paper-live mode requires IBKR Gateway — use run_ibkr.py for live feed")
        logger.info("This script demonstrates the comparison framework with sample data")
        sys.exit(0)


if __name__ == "__main__":
    main()
