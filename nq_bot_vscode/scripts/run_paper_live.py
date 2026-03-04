"""
IBKR Paper Trading Runner with Safety Rails
==============================================
Main entry point for paper trading via IBKR Client Portal Gateway.

Pipeline:
  IBKRClient -> IBKRDataFeed -> CandleAggregator -> 2m Bar
    -> process_bar() (HTF gate + HC filter + institutional modifiers)
      -> Safety Rails check -> Paper order execution
        -> PaperTradingMonitor (statistics, persistence)

Startup sequence:
  1. Initialize IBKR connection (Client Portal Gateway)
  2. Subscribe to MNQ data feed
  3. Initialize HTF engine, confluence scorer, modifier engine
  4. Initialize safety rails
  5. Initialize paper trading monitor
  6. Start main loop

Shutdown (Ctrl+C / SIGINT / SIGTERM):
  Save state, close connections, print final summary

Usage:
    python scripts/run_paper_live.py
    python scripts/run_paper_live.py --dry-run
    python scripts/run_paper_live.py --max-daily-loss 300
    python scripts/run_paper_live.py --log-level DEBUG

Requires .env (or env vars) with:
    IBKR_GATEWAY_HOST   (default: localhost)
    IBKR_GATEWAY_PORT   (default: 5000)
    IBKR_ACCOUNT_TYPE   paper | live
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import random
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

# ── Project path setup ──
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

# ── Load .env before any project imports ──
_env_path = project_dir / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ.setdefault(key.strip(), val)

from config.settings import CONFIG
from features.engine import Bar
from main import TradingOrchestrator, HTF_TIMEFRAMES
from execution.safety_rails import SafetyRails, SafetyRailsConfig
from scripts.paper_trading_monitor import PaperTradingMonitor

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════
LOGS_DIR = project_dir / "logs"
ET_TZ = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# DRY-RUN DATA GENERATOR
# ═══════════════════════════════════════════════════════════════

class DryRunDataGenerator:
    """
    Generates synthetic MNQ-like 2-minute bars for --dry-run mode.
    No IBKR connection required.
    """

    def __init__(self, base_price: float = 20000.0):
        self._price = base_price
        self._bar_count = 0

    def generate_bar(self) -> Bar:
        """Generate a single synthetic 2-minute bar."""
        # Random walk with slight mean reversion
        move = random.gauss(0, 8.0)  # ~8pt std dev per 2m bar (MNQ-like)
        mean_revert = (20000.0 - self._price) * 0.001
        self._price += move + mean_revert

        open_price = self._price
        high = open_price + abs(random.gauss(0, 5.0))
        low = open_price - abs(random.gauss(0, 5.0))
        close = open_price + random.gauss(0, 3.0)
        volume = max(100, int(random.gauss(1500, 500)))

        self._bar_count += 1

        return Bar(
            timestamp=datetime.now(timezone.utc),
            open=round(open_price, 2),
            high=round(max(high, open_price, close), 2),
            low=round(min(low, open_price, close), 2),
            close=round(close, 2),
            volume=volume,
        )


# ═══════════════════════════════════════════════════════════════
# PAPER TRADING RUNNER
# ═══════════════════════════════════════════════════════════════

class PaperLiveRunner:
    """
    Main paper trading runner with IBKR connectivity and safety rails.

    Coordinates:
      - IBKR data feed (or dry-run synthetic data)
      - TradingOrchestrator.process_bar()
      - SafetyRails (circuit breakers)
      - PaperTradingMonitor (statistics)
    """

    # Dashboard refresh interval
    DASHBOARD_INTERVAL_SECONDS = 120

    def __init__(
        self,
        dry_run: bool = False,
        max_daily_loss: float = 500.0,
        log_level: str = "INFO",
    ):
        self._dry_run = dry_run
        self._max_daily_loss = max_daily_loss
        self._log_level = log_level

        # Core components (initialized in start())
        self._bot: Optional[TradingOrchestrator] = None
        self._ibkr_client = None
        self._ibkr_data_feed = None

        # Safety rails
        self._safety_rails = SafetyRails(SafetyRailsConfig(
            max_daily_loss=max_daily_loss,
            max_consecutive_losses=5,
            max_position_size=2,       # ABSOLUTE — no exceptions
            heartbeat_alert_seconds=60.0,
            heartbeat_halt_seconds=300.0,
            log_dir=str(LOGS_DIR),
        ))

        # Paper trading monitor
        self._monitor = PaperTradingMonitor(
            log_dir=str(LOGS_DIR),
            account_size=CONFIG.risk.account_size,
        )

        # Dry-run data generator
        self._dry_gen = DryRunDataGenerator() if dry_run else None

        # State
        self._shutdown_event = asyncio.Event()
        self._bars_processed = 0
        self._trades_executed = 0
        self._last_dashboard_time: float = 0.0

    # ──────────────────────────────────────────────────────────
    # STARTUP
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Full startup sequence."""
        self._print_banner()
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # a. Initialize TradingOrchestrator
        self._bot = TradingOrchestrator(CONFIG)
        await self._bot.initialize(skip_db=True)
        logger.info("TradingOrchestrator initialized (skip_db=True)")

        # b. Connect IBKR (or skip for dry-run)
        if not self._dry_run:
            connected = await self._connect_ibkr()
            if not connected:
                logger.critical("IBKR connection failed — exiting")
                sys.exit(1)
        else:
            logger.info("DRY-RUN mode — using synthetic data (no IBKR)")

        # c. Safety rails already initialized in __init__
        logger.info("Safety rails initialized:")
        logger.info("  Max daily loss:       $%.2f", self._max_daily_loss)
        logger.info("  Max consecutive loss: %d", self._safety_rails.consecutive_losses.max_consecutive)
        logger.info("  Max position size:    %d contracts (ABSOLUTE)", self._safety_rails.position_size.max_contracts)
        logger.info("  Heartbeat alert:      %.0fs", self._safety_rails.heartbeat.alert_seconds)
        logger.info("  Heartbeat halt:       %.0fs", self._safety_rails.heartbeat.halt_seconds)

        # d. Monitor already initialized in __init__
        logger.info("Paper trading monitor initialized")

        # e. Start main loop
        logger.info("=" * 60)
        logger.info("  PAPER TRADING ACTIVE")
        logger.info("=" * 60)

        await self._run_loop()

    async def _connect_ibkr(self) -> bool:
        """Initialize IBKR connection and data feed."""
        try:
            from Broker.ibkr_client import IBKRClient, IBKRDataFeed, IBKRConfig

            ibkr_config = IBKRConfig(
                gateway_host=os.environ.get("IBKR_GATEWAY_HOST", "localhost"),
                gateway_port=int(os.environ.get("IBKR_GATEWAY_PORT", "5000")),
                account_type=os.environ.get("IBKR_ACCOUNT_TYPE", "paper"),
                symbol=os.environ.get("IBKR_SYMBOL", "MNQ"),
            )

            # Safety: warn if live
            if ibkr_config.is_live:
                logger.warning("=" * 60)
                logger.warning("  *** LIVE ACCOUNT DETECTED ***")
                logger.warning("  Paper trading runner should use PAPER account")
                logger.warning("=" * 60)

            self._ibkr_client = IBKRClient(ibkr_config)
            connected = await self._ibkr_client.connect()
            if not connected:
                return False

            # Resolve MNQ contract
            resolved = await self._ibkr_client.resolve_contract()
            if not resolved:
                logger.error("Failed to resolve MNQ contract")
                return False

            # Start data feed
            self._ibkr_data_feed = IBKRDataFeed(self._ibkr_client)
            self._ibkr_data_feed.on_bar(self._on_ibkr_bar)
            started = await self._ibkr_data_feed.start()
            if not started:
                logger.error("Failed to start IBKR data feed")
                return False

            logger.info("IBKR connected: %s:%d (%s)",
                        ibkr_config.gateway_host,
                        ibkr_config.gateway_port,
                        ibkr_config.account_type)
            return True

        except ImportError:
            logger.error("aiohttp not installed. Required for IBKR. Use --dry-run instead.")
            return False
        except Exception as e:
            logger.error("IBKR connection failed: %s", e)
            return False

    def _print_banner(self) -> None:
        logger.info("=" * 60)
        logger.info("  IBKR PAPER TRADING RUNNER (with Safety Rails)")
        logger.info("  Mode:           %s", "DRY-RUN (synthetic data)" if self._dry_run else "LIVE DATA")
        logger.info("  Max daily loss: $%.2f", self._max_daily_loss)
        logger.info("  Max pos size:   2 contracts (ABSOLUTE)")
        logger.info("  HC filter:      score>=0.75, stop<=30pts")
        logger.info("  HTF gate:       strength>=0.3")
        logger.info("  Log level:      %s", self._log_level)
        logger.info("=" * 60)

    # ──────────────────────────────────────────────────────────
    # BAR PROCESSING
    # ──────────────────────────────────────────────────────────

    def _on_ibkr_bar(self, bar: Bar) -> None:
        """Callback from IBKR data feed — schedule async processing."""
        asyncio.get_event_loop().create_task(self._process_bar_safe(bar))

    async def _process_bar_safe(self, bar: Bar) -> None:
        """Process a bar with full error handling."""
        try:
            await self._process_bar(bar)
        except Exception as e:
            logger.error("Error processing bar: %s", e, exc_info=True)

    async def _process_bar(self, bar: Bar) -> None:
        """Process a single bar through the full pipeline with safety rails."""
        self._bars_processed += 1

        # Update heartbeat
        self._safety_rails.on_bar_received()

        # Check market hours for heartbeat
        et_now = bar.timestamp.astimezone(ET_TZ)
        t = et_now.hour + et_now.minute / 60.0
        is_market_hours = 9.5 <= t < 16.0
        self._safety_rails.set_market_hours(is_market_hours)

        # Check safety rails BEFORE processing
        if not self._safety_rails.check_all():
            status = self._safety_rails.get_status()
            if self._bars_processed % 60 == 1:  # Log every ~2 min (60 bars at 2s)
                logger.warning("Safety rails HALTED trading: %s", status)
            return

        # Route through process_bar()
        result = await self._bot.process_bar(bar)

        if result:
            action = result.get("action", "")

            if action == "entry":
                # Clamp position size through safety guard
                requested = result.get("contracts", 2)
                allowed = self._safety_rails.clamp_position_size(requested)
                result["contracts"] = allowed

                self._trades_executed += 1
                logger.info(
                    "ENTRY: %s @ %.2f | Stop: %.2f | Score: %.3f | Source: %s",
                    result.get("direction", "?").upper(),
                    result.get("entry_price", 0),
                    result.get("stop", 0),
                    result.get("signal_score", 0),
                    result.get("signal_source", "?"),
                )

            elif action == "trade_closed":
                pnl = result.get("total_pnl", 0)

                # Record through safety rails
                tripped = self._safety_rails.record_trade(pnl)

                # Record in monitor
                self._monitor.record_trade(
                    pnl=pnl,
                    direction=result.get("direction", ""),
                    entry_price=result.get("entry_price", 0),
                    exit_price=result.get("exit_price", 0),
                    signal_score=result.get("signal_score", 0),
                    signal_source=result.get("signal_source", ""),
                    regime=result.get("regime", ""),
                    htf_bias=result.get("htf_bias", ""),
                    c1_pnl=result.get("c1_pnl", 0),
                    c2_pnl=result.get("c2_pnl", 0),
                    contracts=result.get("contracts", 2),
                    metadata={
                        "inst_position_mult": result.get("inst_position_mult"),
                        "inst_stop_mult": result.get("inst_stop_mult"),
                    },
                )

                logger.info(
                    "TRADE CLOSED: PnL $%.2f | C1: $%.2f | C2: $%.2f | Daily: $%.2f",
                    pnl,
                    result.get("c1_pnl", 0),
                    result.get("c2_pnl", 0),
                    self._safety_rails.daily_loss.daily_pnl,
                )

                if tripped:
                    logger.critical("SAFETY RAIL TRIPPED — trading halted")

        # Periodic monitor update
        self._monitor.update()

    # ──────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main event loop."""
        try:
            while not self._shutdown_event.is_set():
                if self._dry_run:
                    # Generate synthetic bar every 2 seconds
                    bar = self._dry_gen.generate_bar()
                    await self._process_bar(bar)
                    await asyncio.sleep(2.0)
                else:
                    # Bars arrive via IBKR callback, just monitor
                    await asyncio.sleep(1.0)

                    # Check heartbeat
                    if not self._safety_rails.heartbeat.check():
                        logger.critical("Heartbeat HALT — no data received")
                        break

                # Dashboard refresh
                now = time.monotonic()
                if now - self._last_dashboard_time >= self.DASHBOARD_INTERVAL_SECONDS:
                    self._print_dashboard()
                    self._last_dashboard_time = now

                # Check if any breaker tripped
                if not self._safety_rails.check_all():
                    status = self._safety_rails.get_status()
                    logger.critical("Safety rails HALTED — require manual reset")
                    logger.info("Status: %s", json.dumps(status, indent=2))
                    # Continue loop but don't trade — wait for shutdown
                    await asyncio.sleep(5.0)

        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    # ──────────────────────────────────────────────────────────
    # DASHBOARD
    # ──────────────────────────────────────────────────────────

    def _print_dashboard(self) -> None:
        """Print status dashboard."""
        if not self._bot:
            return

        stats = self._monitor.get_stats()
        rail_status = self._safety_rails.get_status()
        pf = stats.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf is not None else "inf"

        et_now = datetime.now(ET_TZ)
        et_str = et_now.strftime("%H:%M:%S ET")

        W = 62
        bar = "=" * W

        lines = [
            "",
            bar,
            f"  PAPER TRADING  {et_str}",
            bar,
            "",
            f"  BARS         {self._bars_processed}",
            f"  TRADES       {stats['trade_count']}  ({stats['wins']}W / {stats['losses']}L)",
            f"  PnL          ${stats['total_pnl']:+.2f}",
            f"  Win Rate     {stats['win_rate']:.1f}%",
            f"  Profit Fac   {pf_str}",
            f"  Sharpe       {stats['sharpe_estimate']:.2f}",
            f"  Drawdown     ${stats['current_drawdown']:.2f} (max: ${stats['max_drawdown']:.2f})",
            "",
            f"  SAFETY       {'OK' if rail_status['trading_allowed'] else 'HALTED'}",
            f"  Daily PnL    ${self._safety_rails.daily_loss.daily_pnl:+.2f}  (limit: -${self._max_daily_loss:.0f})",
            f"  Consec Loss  {self._safety_rails.consecutive_losses.consecutive_losses} / {self._safety_rails.consecutive_losses.max_consecutive}",
            "",
            bar,
        ]
        print("\n".join(lines))

    # ──────────────────────────────────────────────────────────
    # SHUTDOWN
    # ──────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Graceful shutdown: save state, close connections, print summary."""
        logger.info("=" * 60)
        logger.info("  SHUTDOWN INITIATED")
        logger.info("=" * 60)

        # Flatten any open positions
        if self._bot and self._bot.executor.has_active_trade:
            logger.warning("Flattening open position before shutdown")
            price = self._bot._get_last_price()
            if price > 0:
                result = await self._bot.executor.emergency_flatten(price)
                if result:
                    pnl = result.get("total_pnl", 0)
                    self._safety_rails.record_trade(pnl)
                    self._monitor.record_trade(
                        pnl=pnl,
                        direction=result.get("direction", ""),
                        metadata={"exit_reason": "shutdown_flatten"},
                    )

        # Save monitor state
        self._monitor.save_state()

        # Print final summary
        self._monitor.print_dashboard()
        self._print_safety_summary()

        # Close IBKR connections
        if self._ibkr_data_feed:
            try:
                await self._ibkr_data_feed.stop()
            except Exception as e:
                logger.warning("Error stopping data feed: %s", e)

        if self._ibkr_client:
            try:
                await self._ibkr_client.disconnect()
            except Exception as e:
                logger.warning("Error disconnecting IBKR: %s", e)

        # Close orchestrator
        if self._bot:
            try:
                await self._bot.shutdown()
            except Exception as e:
                logger.warning("Error shutting down bot: %s", e)

        logger.info("Shutdown complete — all state saved")

    def _print_safety_summary(self) -> None:
        """Print safety rail status summary."""
        status = self._safety_rails.get_status()
        print(f"\n  SAFETY RAILS: {'ALL OK' if status['trading_allowed'] else 'HALTED'}")
        print(f"  Daily loss breaker:     {'TRIPPED' if status['daily_loss']['tripped'] else 'OK'}")
        print(f"  Consecutive losses:     {'TRIPPED' if status['consecutive_losses']['tripped'] else 'OK'}")
        print(f"  Heartbeat:              {'TRIPPED' if status['heartbeat']['tripped'] else 'OK'}")
        print(f"  Position size clamps:   {status['position_size']['clamp_count']}")

    def request_shutdown(self) -> None:
        """Called from signal handler."""
        self._shutdown_event.set()


# ═══════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IBKR Paper Trading Runner — MNQ with safety rails",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python scripts/run_paper_live.py                     # Live IBKR paper trading
  python scripts/run_paper_live.py --dry-run            # Synthetic data, no IBKR
  python scripts/run_paper_live.py --max-daily-loss 300  # Override daily loss limit
  python scripts/run_paper_live.py --log-level DEBUG    # Verbose logging

Safety Rails:
  - Max daily loss:       $500 (configurable via --max-daily-loss)
  - Max consecutive loss: 5 trades -> HALT
  - Max position size:    2 contracts (ABSOLUTE, not configurable)
  - Heartbeat timeout:    60s alert, 300s halt

All circuit breakers require manual restart after tripping.
""",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without IBKR connection (uses random synthetic data)",
    )
    parser.add_argument(
        "--max-daily-loss", type=float, default=500.0,
        help="Maximum daily loss before halting (default: $500)",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    # Create log directory
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Configure logging
    log_level = getattr(logging, args.log_level)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        str(LOGS_DIR / "paper_live.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(file_handler)

    runner = PaperLiveRunner(
        dry_run=args.dry_run,
        max_daily_loss=args.max_daily_loss,
        log_level=args.log_level,
    )

    # Event loop with signal handlers
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("Shutdown signal received (SIGINT/SIGTERM)")
        runner.request_shutdown()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    SHUTDOWN_TIMEOUT_SECONDS = 30

    try:
        loop.run_until_complete(runner.start())
    except KeyboardInterrupt:
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            logger.critical(
                "Shutdown timed out after %ds — MANUAL CHECK REQUIRED",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
    except Exception:
        logger.critical(
            "UNHANDLED EXCEPTION — shutting down\n%s",
            traceback.format_exc(),
        )
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            logger.critical(
                "Shutdown timed out after %ds — MANUAL CHECK REQUIRED",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
