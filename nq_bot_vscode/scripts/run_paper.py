"""
Paper Trading Runner — Config D Live on Tradovate Demo
========================================================
Feeds real-time 2m bars from Tradovate demo into the SAME pipeline
used by the backtester. Zero parameter changes.

Pipeline:
  Tradovate WebSocket (1m bars)
    -> TradovatePaperConnector (aggregates 1m -> 2m)
      -> TradingOrchestrator.process_bar() (HC filter + HTF gate)
        -> ScaleOutExecutor (trade lifecycle)
          -> TradovatePaperConnector (demo orders)

Session rules:
  - No entries before 6:01 PM ET
  - Flat by 4:30 PM ET
  - No trading during maintenance (5:00–6:00 PM ET)
  - Daily loss limit: $500 -> halt
  - Connection loss > 60s -> flatten + halt

Usage:
    python scripts/run_paper.py
    python scripts/run_paper.py --symbol MNQM5
    python scripts/run_paper.py --dry-run      # Connect but don't trade

Requires .env with:
    TRADOVATE_USERNAME, TRADOVATE_PASSWORD, TRADOVATE_APP_ID,
    TRADOVATE_CID, TRADOVATE_SECRET, TRADOVATE_DEVICE_ID
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict
from zoneinfo import ZoneInfo

# Ensure project root is on path
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

# Load .env if present
_env_path = project_dir / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                # Strip matching surrounding quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ.setdefault(key.strip(), val)

from config.settings import CONFIG
from features.engine import Bar
from data_pipeline.pipeline import BarData, bardata_to_bar, bardata_to_htfbar
from main import TradingOrchestrator, HTF_TIMEFRAMES
from execution.tradovate_paper import (
    TradovatePaperConnector,
    DAILY_LOSS_LIMIT_DOLLARS,
    MAX_POSITION_CONTRACTS,
)

logger = logging.getLogger(__name__)

LOGS_DIR = project_dir / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DECISION_LOG_PATH = str(LOGS_DIR / "paper_decisions.json")

# OOS baseline for comparison (Config D + C1 Time Exit, Sep 2025 – Feb 2026)
OOS_EXPECTANCY = 15.34    # $/trade from 6-month OOS
OOS_WIN_RATE = 68.1       # % from 6-month OOS
OOS_TRADES_PER_MONTH = 158


class PaperTradingRunner:
    """
    Orchestrates the paper trading session.

    Connects to Tradovate demo, feeds bars through the backtest pipeline,
    enforces session rules, logs every decision.
    """

    def __init__(self, dry_run: bool = False):
        # ── SAFETY: Force demo + paper mode ──
        CONFIG.tradovate.environment = "demo"
        CONFIG.execution.paper_trading = True

        self.dry_run = dry_run
        self.connector = TradovatePaperConnector(CONFIG)
        self.bot: Optional[TradingOrchestrator] = None

        # Decision log
        self._decisions: list = []
        self._bars_processed = 0
        self._session_start: Optional[datetime] = None
        self._daily_summary_sent = False

        # Graceful shutdown
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Initialize pipeline and connect to Tradovate demo."""
        logger.info("=" * 60)
        logger.info("  PAPER TRADING — CONFIG D + C1 TRAIL FROM PROFIT")
        logger.info("  HC filter: score>=0.75, stop<=30pts")
        logger.info("  C1 exit: trail from +3pts (trail 2.5pts, fallback 12 bars)")
        logger.info("  HTF gate: strength>=0.3")
        logger.info(f"  Max contracts: {MAX_POSITION_CONTRACTS}")
        logger.info(f"  Daily loss limit: ${DAILY_LOSS_LIMIT_DOLLARS}")
        logger.info(f"  Dry run: {self.dry_run}")
        logger.info("=" * 60)

        # Initialize the orchestrator (same as backtest)
        self.bot = TradingOrchestrator(CONFIG)
        await self.bot.initialize(skip_db=True)

        # Register the 2m bar callback
        self.connector._on_2m_bar = self._on_2m_bar

        # Connect to Tradovate demo
        if not await self.connector.connect():
            logger.error("Failed to connect. Check credentials in .env")
            return

        self._session_start = datetime.now(timezone.utc)
        logger.info("Paper trading session started")
        self._log_decision("session_start", {
            "config": "D",
            "hc_filter": "ON",
            "htf_gate": 0.3,
            "dry_run": self.dry_run,
        })

        # Run until shutdown
        await self._run_loop()

    async def _run_loop(self) -> None:
        """Main event loop — runs until shutdown signal."""
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(1)

                # Check if connector halted
                if self.connector.state.is_halted:
                    logger.warning(
                        f"Connector halted: {self.connector.state.halt_reason}"
                    )
                    break

                # Check session boundaries
                et_now = TradovatePaperConnector.get_et_now()

                # Flat-by time check
                if TradovatePaperConnector.should_be_flat(et_now):
                    if self.bot and self.bot.executor.has_active_trade:
                        logger.info("Session close approaching — flattening")
                        self._log_decision("session_flatten", {
                            "time_et": et_now.strftime("%H:%M"),
                        })
                        price = self.bot._get_last_price()
                        if price > 0:
                            result = await self.bot.executor.emergency_flatten(price)
                            if result:
                                self.connector.record_trade_pnl(
                                    result.get("total_pnl", 0), result
                                )

                # Daily summary at 4:45 PM ET
                if et_now.hour == 16 and et_now.minute >= 45 and not self._daily_summary_sent:
                    self._print_daily_summary()
                    self._daily_summary_sent = True

                # Daily reset at 6:00 PM ET (new session)
                if et_now.hour == 18 and et_now.minute == 0:
                    self.connector.reset_daily_state()
                    self._daily_summary_sent = False

        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _on_2m_bar(self, bar_data: Dict) -> None:
        """
        Called when a complete 2-minute bar arrives.
        Routes through the SAME pipeline as the backtester.
        """
        if not self.bot or not self.bot._running:
            return

        # Check session rules
        et_now = TradovatePaperConnector.get_et_now()
        if not TradovatePaperConnector.is_within_session(et_now):
            self._log_decision("bar_skipped", {
                "reason": "outside_session",
                "time_et": et_now.strftime("%H:%M"),
            })
            return

        # Convert to Bar object (same format as backtest)
        try:
            bar = Bar(
                timestamp=bar_data["timestamp"],
                open=bar_data["open"],
                high=bar_data["high"],
                low=bar_data["low"],
                close=bar_data["close"],
                volume=bar_data["volume"],
            )
        except (KeyError, TypeError) as e:
            logger.warning(f"Bad bar data: {e}")
            return

        self._bars_processed += 1

        # Check daily loss limit before processing
        if self.connector.state.daily_loss_limit_hit:
            self._log_decision("bar_skipped", {
                "reason": "daily_loss_limit",
                "daily_pnl": self.connector.state.daily_pnl,
            })
            return

        # Check flat-by time — no new entries
        if TradovatePaperConnector.should_be_flat(et_now):
            # Still update active positions
            if self.bot.executor.has_active_trade:
                result = await self.bot.process_bar(bar)
                if result:
                    self._handle_result(result, bar)
            return

        # Dry run: process bar but don't execute trades
        if self.dry_run:
            self._log_decision("bar_processed_dry", {
                "close": bar.close,
                "volume": bar.volume,
                "bars_total": self._bars_processed,
            })
            return

        # ── CORE: Same process_bar() as backtest ──
        result = await self.bot.process_bar(bar)

        if result:
            self._handle_result(result, bar)
        else:
            # Log blocks/skips (every 10 bars to avoid noise)
            if self._bars_processed % 10 == 0:
                self._log_decision("no_signal", {
                    "bar_num": self._bars_processed,
                    "close": bar.close,
                    "has_position": self.bot.executor.has_active_trade,
                })

    def _handle_result(self, result: Dict, bar: Bar) -> None:
        """Handle a trade action from process_bar()."""
        action = result.get("action", "")

        if action == "entry":
            self._log_decision("entry", {
                "direction": result.get("direction"),
                "entry_price": result.get("entry_price"),
                "stop": result.get("stop"),
                "c1_exit_rule": result.get("c1_exit_rule"),
                "signal_score": result.get("signal_score"),
                "regime": result.get("regime"),
                "htf_bias": result.get("htf_bias"),
                "htf_strength": result.get("htf_strength"),
            })
            logger.info(
                f"ENTRY: {result.get('direction', '?').upper()} @ "
                f"{result.get('entry_price', 0):.2f} | "
                f"Stop: {result.get('stop', 0):.2f} | "
                f"C1: {result.get('c1_exit_rule', 'time_10bars')} | "
                f"Score: {result.get('signal_score', 0):.3f}"
            )

        elif action == "c1_time_exit":
            self._log_decision("c1_time_exit", {
                "c1_pnl": result.get("c1_pnl"),
                "c1_bars": result.get("c1_bars"),
                "c2_new_stop": result.get("c2_new_stop"),
                "price": result.get("price"),
            })

        elif action == "trade_closed":
            pnl = result.get("total_pnl", 0)
            self.connector.record_trade_pnl(pnl, result)
            self._log_decision("trade_closed", {
                "direction": result.get("direction"),
                "entry_price": result.get("entry_price"),
                "total_pnl": pnl,
                "c1_pnl": result.get("c1_pnl"),
                "c2_pnl": result.get("c2_pnl"),
                "c1_reason": result.get("c1_exit_reason"),
                "c2_reason": result.get("c2_exit_reason"),
                "daily_pnl": self.connector.state.daily_pnl,
            })
            logger.info(
                f"TRADE CLOSED: PnL ${pnl:.2f} | "
                f"Daily ${self.connector.state.daily_pnl:.2f} | "
                f"C1: {result.get('c1_exit_reason')} ${result.get('c1_pnl', 0):.2f} | "
                f"C2: {result.get('c2_exit_reason')} ${result.get('c2_pnl', 0):.2f}"
            )

    def _print_daily_summary(self) -> None:
        """Print end-of-day summary comparing to OOS expectancy."""
        state = self.connector.state
        stats = self.bot.executor.get_stats() if self.bot else {}
        total_trades = stats.get("total_trades", 0)

        print(f"\n{'=' * 60}")
        print(f"  DAILY SUMMARY — {state.session_date}")
        print(f"{'=' * 60}")
        print(f"  Bars processed:     {self._bars_processed}")
        print(f"  Trades today:       {state.daily_trades}")
        print(f"  Daily PnL:          ${state.daily_pnl:+.2f}")
        print(f"  Loss limit hit:     {'YES' if state.daily_loss_limit_hit else 'No'}")
        print(f"  Halted:             {'YES — ' + state.halt_reason if state.is_halted else 'No'}")

        if state.daily_trades > 0:
            today_exp = state.daily_pnl / state.daily_trades
            print(f"  Expectancy/trade:   ${today_exp:+.2f}  (OOS baseline: ${OOS_EXPECTANCY:.2f})")
            exp_delta = today_exp - OOS_EXPECTANCY
            indicator = "ABOVE" if exp_delta >= 0 else "BELOW"
            print(f"  vs OOS baseline:    ${exp_delta:+.2f} ({indicator})")

        print(f"\n  Session totals:")
        print(f"  Total trades:       {total_trades}")
        wr = stats.get("win_rate", 0)
        pf = stats.get("profit_factor", 0)
        total_pnl = stats.get("total_pnl", 0)
        print(f"  Win rate:           {wr:.1f}%  (OOS baseline: {OOS_WIN_RATE}%)")
        print(f"  Profit factor:      {pf:.2f}")
        print(f"  Total PnL:          ${total_pnl:+.2f}")
        print(f"  C1 PnL:             ${stats.get('c1_total_pnl', 0):+.2f}")
        print(f"  C2 PnL:             ${stats.get('c2_total_pnl', 0):+.2f}")
        print(f"{'=' * 60}\n")

        self._log_decision("daily_summary", {
            "date": state.session_date,
            "daily_pnl": state.daily_pnl,
            "daily_trades": state.daily_trades,
            "total_trades": total_trades,
            "win_rate": wr,
            "profit_factor": pf,
            "total_pnl": total_pnl,
        })

    # ================================================================
    # DECISION LOGGING
    # ================================================================
    def _log_decision(self, decision_type: str, data: Dict) -> None:
        """Log a trading decision to paper_decisions.json."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": decision_type,
            **data,
        }
        self._decisions.append(entry)

        # Flush every 20 decisions
        if len(self._decisions) % 20 == 0:
            self._flush_decisions()

    def _flush_decisions(self) -> None:
        """Write decisions to disk."""
        if not self._decisions:
            return
        try:
            existing = []
            if os.path.exists(DECISION_LOG_PATH):
                with open(DECISION_LOG_PATH, "r") as f:
                    existing = json.load(f)
            existing.extend(self._decisions)
            with open(DECISION_LOG_PATH, "w") as f:
                json.dump(existing, f, indent=2, default=str)
            self._decisions.clear()
        except Exception as e:
            logger.error(f"Failed to write decision log: {e}")

    # ================================================================
    # SHUTDOWN
    # ================================================================
    async def shutdown(self) -> None:
        """Graceful shutdown — flatten, disconnect, log."""
        logger.info("Shutting down paper trading session...")

        # Flatten any open positions
        if self.bot and self.bot.executor.has_active_trade:
            price = self.bot._get_last_price()
            if price > 0:
                logger.info("Flattening open position before shutdown")
                result = await self.bot.executor.emergency_flatten(price)
                if result:
                    self.connector.record_trade_pnl(
                        result.get("total_pnl", 0), result
                    )

        self._print_daily_summary()
        self._flush_decisions()

        await self.connector.disconnect()

        if self.bot:
            await self.bot.shutdown()

        logger.info("Paper trading session ended")

    def request_shutdown(self) -> None:
        """Signal shutdown from signal handler."""
        self._shutdown_event.set()


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Runner — Config D")
    parser.add_argument(
        "--symbol", type=str, default=None,
        help="Override trading symbol (default: from config)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Connect and process bars but don't execute trades"
    )
    args = parser.parse_args()

    if args.symbol:
        CONFIG.tradovate.symbol = args.symbol

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                str(LOGS_DIR / "paper_trading.log"),
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
            ),
        ],
    )

    runner = PaperTradingRunner(dry_run=args.dry_run)

    # Handle Ctrl+C gracefully
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("Shutdown signal received")
        runner.request_shutdown()

    # add_signal_handler is Unix-only; on Windows, fall through to
    # the KeyboardInterrupt handler below.
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    else:
        logger.info(
            "Windows detected — using KeyboardInterrupt for Ctrl+C shutdown"
        )

    try:
        loop.run_until_complete(runner.start())
    except KeyboardInterrupt:
        loop.run_until_complete(runner.shutdown())
    except Exception:
        # NEVER leave positions open on crash
        logger.critical(
            "UNHANDLED EXCEPTION — flattening all positions\n%s",
            traceback.format_exc(),
        )
        loop.run_until_complete(runner.shutdown())
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
