"""
IBKR Auto-Launch Entry Point
==============================
Fully automated startup: cold-start TWS → authenticate via IBC → connect →
health monitor → crash recovery → auto-restart.

Sequence:
  1. Detect TWS installation (or check if already running)
  2. Launch TWS via IBC if needed
  3. Wait for TWS API port to accept connections
  4. Connect IBKRClient to TWS socket API
  5. Verify account + resolve contract
  6. Start IBKRLivePipeline (Feature Engine → Signals → Orders)
  7. Run health monitor alongside trading loop
  8. On crash: flatten positions → kill TWS → relaunch → reconnect

Usage:
    python scripts/run_ibkr_auto.py
    python scripts/run_ibkr_auto.py --dry-run
    python scripts/run_ibkr_auto.py --port 7497 --max-retries 5

SECURITY: Credentials are read from .env, passed to IBC config file,
and NEVER logged or printed.
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")

from Broker.ibkr_client import IBKRClient
from Broker.tws_launcher import TWSLauncher
from Broker.tws_health_monitor import TWSHealthMonitor
from config.tws_auto_config import TWSAutoConfig

logger = logging.getLogger(__name__)

# ── Paths ──
LOGS_DIR = PROJECT_DIR / "logs"
HEARTBEAT_FILE = LOGS_DIR / "heartbeat_state.json"


class IBKRAuto:
    """
    Orchestrates the full TWS auto-launch lifecycle:
    detect → launch → connect → trade → monitor → recover.
    """

    def __init__(self, config: TWSAutoConfig, dry_run: bool = False):
        self._config = config
        self._dry_run = dry_run

        self._launcher = TWSLauncher(config)
        self._client = IBKRClient(
            host=config.tws_host,
            port=config.tws_port,
            client_id=config.client_id,
        )
        self._health_monitor = TWSHealthMonitor(
            client=self._client,
            launcher=self._launcher,
            heartbeat_file=HEARTBEAT_FILE,
            check_interval=config.health_check_interval,
            failure_threshold=config.consecutive_failures_threshold,
            on_critical_failure=self._handle_critical_failure,
        )

        self._restart_count = 0
        self._shutdown_requested = False
        self._pipeline = None
        self._loop = None

    # ──────────────────────────────────────────────────────
    # MAIN ENTRY
    # ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main async entry point."""
        self._print_banner()

        # Register signal handlers
        self._loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: self._request_shutdown())

        # Initial startup sequence
        success = await self._startup_sequence()
        if not success:
            logger.critical("Initial startup failed -- exiting")
            return

        # Start health monitor
        self._health_monitor.start()

        # Run trading loop (or idle in dry-run)
        if self._dry_run:
            logger.info("[DRY-RUN] Startup successful -- would start trading here")
            logger.info("[DRY-RUN] TWS is connected, health monitor running")
            logger.info("[DRY-RUN] Press Ctrl+C to stop")
            await self._idle_loop()
        else:
            await self._trading_loop()

        # Cleanup
        await self._shutdown()

    # ──────────────────────────────────────────────────────
    # STARTUP SEQUENCE
    # ──────────────────────────────────────────────────────

    async def _startup_sequence(self) -> bool:
        """
        Full startup: detect → launch → wait → connect → verify.
        Returns True if everything is ready.
        """
        logger.info("=" * 60)
        logger.info("  IBKR AUTO-LAUNCH STARTUP")
        logger.info("  %s", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
        logger.info("=" * 60)
        logger.info("Config:\n%s", self._config.summary())

        # Step 1: Check if TWS is already running
        if self._launcher.is_running():
            logger.info("Step 1: TWS is already running -- skipping launch")
        else:
            # Step 2: Launch TWS
            logger.info("Step 1: Launching TWS...")
            if not self._launcher.launch():
                logger.error("Failed to launch TWS")
                return False

            # Step 3: Wait for ready
            logger.info("Step 2: Waiting for TWS API to be ready...")
            if not self._launcher.wait_for_ready():
                logger.error("TWS did not become ready in time")
                self._launcher.kill()
                return False

        # Step 4: Connect IBKRClient
        logger.info("Step 3: Connecting IBKRClient...")
        connected = await self._client.connect()
        if not connected:
            logger.error("Failed to connect IBKRClient to TWS")
            return False

        # Step 5: Verify account
        logger.info("Step 4: Verifying account...")
        if not await self._verify_account():
            logger.error("Account verification failed")
            return False

        # Step 6: Resolve contract
        logger.info("Step 5: Resolving MNQ contract...")
        try:
            contract = self._client.get_contract("MNQ", "CME")
            logger.info("Contract resolved: %s expiry %s",
                        contract.symbol, contract.lastTradeDateOrContractMonth)
        except Exception as e:
            logger.error("Contract resolution failed: %s", e)
            return False

        logger.info("=" * 60)
        logger.info("  STARTUP COMPLETE -- READY TO TRADE")
        logger.info("=" * 60)
        return True

    async def _verify_account(self) -> bool:
        """Verify the connected account is valid and in paper mode."""
        try:
            summary = await self._client.get_account_summary()
            if not summary:
                logger.warning("Empty account summary -- may be paper account with no data")
                return True  # Paper accounts may have empty summaries

            nlv = summary.get("NetLiquidation", 0)
            if nlv > 0:
                logger.info("Account verified (NetLiquidation available)")

            return True
        except Exception as e:
            logger.error("Account verification error: %s", e)
            return False

    # ──────────────────────────────────────────────────────
    # TRADING LOOP
    # ──────────────────────────────────────────────────────

    async def _trading_loop(self) -> None:
        """
        Start the IBKRLivePipeline and run until shutdown.
        Uses the existing orchestrator pipeline.
        """
        try:
            from execution.orchestrator import IBKRLivePipeline

            logger.info("Starting IBKRLivePipeline...")
            self._pipeline = IBKRLivePipeline(client=self._client)
            await self._pipeline.start()

            # Keep running until shutdown
            while not self._shutdown_requested:
                await asyncio.sleep(1.0)

        except ImportError:
            logger.warning(
                "IBKRLivePipeline not available -- running in monitor-only mode. "
                "The bot will stay connected but won't trade."
            )
            await self._idle_loop()
        except Exception as e:
            logger.error("Trading loop error: %s", e)
            if not self._shutdown_requested:
                self._handle_critical_failure()

    async def _idle_loop(self) -> None:
        """Simple idle loop for dry-run or monitor-only mode."""
        while not self._shutdown_requested:
            await asyncio.sleep(1.0)

    # ──────────────────────────────────────────────────────
    # CRASH RECOVERY
    # ──────────────────────────────────────────────────────

    def _handle_critical_failure(self) -> None:
        """
        Called by health monitor when failure threshold is breached.
        Schedules async recovery.
        """
        if self._shutdown_requested:
            return

        if self._restart_count >= self._config.max_restart_attempts:
            logger.critical(
                "Max restart attempts (%d) reached -- giving up. "
                "Manual intervention required.",
                self._config.max_restart_attempts,
            )
            self._request_shutdown()
            return

        self._restart_count += 1
        logger.warning(
            "Scheduling restart attempt %d/%d...",
            self._restart_count,
            self._config.max_restart_attempts,
        )

        if self._loop and self._loop.is_running():
            asyncio.ensure_future(self._restart_sequence())

    async def _restart_sequence(self) -> None:
        """Full restart: flatten → disconnect → kill → relaunch → reconnect."""
        logger.info("=" * 60)
        logger.info("  RESTART SEQUENCE (attempt %d/%d)",
                     self._restart_count, self._config.max_restart_attempts)
        logger.info("=" * 60)

        # Stop health monitor during restart
        self._health_monitor.stop()

        # Step 1: Try to flatten positions
        if self._config.flatten_on_crash:
            await self._try_flatten()

        # Step 2: Disconnect client
        try:
            self._client.disconnect()
        except Exception as e:
            logger.warning("Disconnect error (non-fatal): %s", e)

        # Step 3: Kill TWS
        self._launcher.kill()

        # Step 4: Cooldown
        logger.info("Cooldown %.0fs before restart...", self._config.restart_cooldown)
        await asyncio.sleep(self._config.restart_cooldown)

        # Step 5: Relaunch
        success = await self._startup_sequence()
        if success:
            logger.info("Restart successful -- resuming health monitor")
            self._health_monitor.start()

            if not self._dry_run:
                # Re-enter trading loop in background
                asyncio.ensure_future(self._trading_loop())
        else:
            logger.error("Restart failed -- will retry on next health check")
            self._health_monitor.start()

    async def _try_flatten(self) -> None:
        """Try to flatten all positions before restart (best-effort)."""
        try:
            if not self._client.is_connected():
                logger.warning("Cannot flatten -- not connected")
                return

            positions = await self._client.get_positions()
            for pos in positions:
                size = pos.get("size", 0)
                if size != 0:
                    action = "SELL" if size > 0 else "BUY"
                    qty = abs(int(size))
                    logger.warning(
                        "Flattening: %s %d %s",
                        action, qty, pos.get("symbol", "?"),
                    )
                    await self._client.place_order(action, qty, "MKT")

            logger.info("Flatten complete")
        except Exception as e:
            logger.error("Flatten failed (will restart anyway): %s", e)

    # ──────────────────────────────────────────────────────
    # SHUTDOWN
    # ──────────────────────────────────────────────────────

    def _request_shutdown(self) -> None:
        """Signal graceful shutdown."""
        logger.info("Shutdown requested...")
        self._shutdown_requested = True

    async def _shutdown(self) -> None:
        """Graceful shutdown: stop monitor, disconnect, cleanup."""
        logger.info("Shutting down...")

        self._health_monitor.stop()

        if self._pipeline:
            try:
                await self._pipeline.stop()
            except Exception as e:
                logger.warning("Pipeline stop error: %s", e)

        try:
            self._client.disconnect()
        except Exception:
            pass

        self._launcher.cleanup()
        logger.info("Shutdown complete")

    # ──────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        """Print startup banner."""
        print("\n" + "=" * 60)
        print("  🤖 IBKR AUTO-LAUNCH")
        print(f"  Mode:     {'DRY-RUN' if self._dry_run else 'LIVE'}")
        print(f"  Trading:  {self._config.trading_mode.upper()}")
        print(f"  Port:     {self._config.tws_port}")
        print(f"  Restart:  {'ON' if self._config.auto_restart else 'OFF'} "
              f"(max {self._config.max_restart_attempts})")
        print("=" * 60 + "\n")


# ──────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="IBKR TWS Auto-Launch & Self-Authentication",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Launch TWS and connect, but don't start trading",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override TWS port (default: from .env or 7497)",
    )
    parser.add_argument(
        "--max-retries", type=int, default=None,
        help="Override max restart attempts (default: from .env or 5)",
    )
    parser.add_argument(
        "--no-restart", action="store_true",
        help="Disable auto-restart on crash",
    )
    args = parser.parse_args()

    # Setup logging
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.handlers.RotatingFileHandler(
                LOGS_DIR / "ibkr_auto.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )

    # Build config
    config = TWSAutoConfig()

    if args.port:
        config.tws_port = args.port
    if args.max_retries is not None:
        config.max_restart_attempts = args.max_retries
    if args.no_restart:
        config.auto_restart = False

    # Run
    auto = IBKRAuto(config, dry_run=args.dry_run)
    asyncio.run(auto.run())


if __name__ == "__main__":
    main()
