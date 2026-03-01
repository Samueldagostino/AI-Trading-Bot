"""
IBKR Live Trading Runner
==========================
Entry point for IBKR Client Portal Gateway paper/live trading.

Pipeline (uses IBKRLivePipeline — no logic duplicated):
  IBKRClient → CandleAggregator → candle_to_bar() → Bar
    → Feature Engine → Signal Aggregator → HC Filter → Risk Engine
      → SignalBridge → IBKROrderExecutor → PositionManager
        → Reconciliation loop (30s) → Kill switch if mismatch

Startup sequence:
  1. Load .env, validate required IBKR env vars
  2. IBKRLivePipeline.start() — connection, contract, data feed, recon
  3. Historical bar warmup (2h backfill primes indicators)
  4. WARMUP COMPLETE — TRADING ACTIVE

Shutdown (SIGINT, SIGTERM, unhandled exception):
  1. Stop accepting new bars
  2. Flatten all open positions
  3. Cancel open orders
  4. Stop reconciliation loop
  5. Disconnect from IBKR Gateway

Structured logging:
  logs/ibkr_trades.json    — every fill
  logs/ibkr_decisions.json — every bar's decision
  logs/ibkr_errors.log     — connection issues, API errors

Usage:
    python scripts/run_ibkr.py
    python scripts/run_ibkr.py --dry-run
    python scripts/run_ibkr.py --allow-eth

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
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

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
from Broker.ibkr_client import (
    IBKRConfig,
    IBKRClient,
    IBKRDataFeed,
    get_session_type,
    SessionType,
    ET_OFFSET,
    ET_TZ,
)
from Broker.order_executor import ExecutorConfig
from execution.orchestrator import IBKRLivePipeline, PipelineState

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════
LOGS_DIR = project_dir / "logs"
TRADES_LOG = LOGS_DIR / "ibkr_trades.json"
DECISIONS_LOG = LOGS_DIR / "ibkr_decisions.json"
ERRORS_LOG = LOGS_DIR / "ibkr_errors.log"

# ═══════════════════════════════════════════════════════════════
# REQUIRED ENV VARS
# ═══════════════════════════════════════════════════════════════
REQUIRED_ENV = {
    "IBKR_GATEWAY_HOST": "Hostname of IBKR Client Portal Gateway (e.g. localhost)",
    "IBKR_GATEWAY_PORT": "Port of IBKR Client Portal Gateway (e.g. 5000)",
    "IBKR_ACCOUNT_TYPE": "Account type: 'paper' or 'live'",
}


def validate_env() -> Dict[str, str]:
    """
    Validate all required env vars are set.
    Fail fast with clear error message listing every missing var.
    """
    missing = []
    values = {}
    for key, description in REQUIRED_ENV.items():
        val = os.environ.get(key)
        if not val:
            missing.append(f"  {key:.<35} {description}")
        else:
            values[key] = val

    if missing:
        print("\nFATAL: Missing required environment variables:\n")
        for m in missing:
            print(m)
        print(
            "\nSet them in .env or export them before running.\n"
            "See .env.example for reference.\n"
        )
        sys.exit(1)

    return values


# ═══════════════════════════════════════════════════════════════
# STRUCTURED LOGGERS
# ═══════════════════════════════════════════════════════════════

class JSONLogger:
    """Append-only JSON logger that writes one entry per event."""

    def __init__(self, path: Path):
        self._path = path
        self._buffer: List[dict] = []
        self._flush_interval = 10

    def log(self, entry: dict) -> None:
        entry["logged_at"] = datetime.now(timezone.utc).isoformat()
        self._buffer.append(entry)
        if len(self._buffer) >= self._flush_interval:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        try:
            existing = []
            if self._path.exists():
                with open(self._path, "r") as f:
                    existing = json.load(f)
            existing.extend(self._buffer)
            with open(self._path, "w") as f:
                json.dump(existing, f, indent=2, default=str)
            self._buffer.clear()
        except Exception as e:
            logger.error("Failed to write %s: %s", self._path.name, e)


# ═══════════════════════════════════════════════════════════════
# IBKR LIVE RUNNER
# ═══════════════════════════════════════════════════════════════

class IBKRLiveRunner:
    """
    Top-level runner for IBKR live/paper trading.

    Wraps IBKRLivePipeline with:
      - Env validation and config construction
      - Indicator warmup tracking
      - Session-aware logging (RTH/ETH transitions)
      - Structured JSON logging for trades, decisions, errors
      - Graceful shutdown on SIGINT/SIGTERM/crash
    """

    # Bars needed to prime ATR-14 and 20-bar volume average
    WARMUP_BARS = 30
    # Terminal dashboard refresh interval
    DASHBOARD_INTERVAL_SECONDS = 120

    def __init__(
        self,
        ibkr_config: IBKRConfig,
        executor_config: ExecutorConfig,
        dry_run: bool = False,
    ):
        self._ibkr_config = ibkr_config
        self._executor_config = executor_config
        self._dry_run = dry_run

        self._pipeline: Optional[IBKRLivePipeline] = None
        self._shutdown_event = asyncio.Event()

        # Warmup state
        self._warmup_complete = False
        self._warmup_bar_count = 0

        # Session tracking
        self._last_session: Optional[SessionType] = None
        self._session_date: str = ""

        # Structured loggers
        self._trade_log = JSONLogger(TRADES_LOG)
        self._decision_log = JSONLogger(DECISIONS_LOG)

        # Dashboard timer
        self._last_dashboard_time: float = 0.0

    # ──────────────────────────────────────────────────────────
    # STARTUP
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Full startup sequence: validate → connect → warmup → run."""
        self._print_banner()

        # Build pipeline
        self._pipeline = IBKRLivePipeline(
            bot_config=CONFIG,
            ibkr_config=self._ibkr_config,
            executor_config=self._executor_config,
        )

        # Intercept bar callbacks for warmup + logging
        original_on_bar = self._pipeline._on_bar
        self._pipeline._data_feed.on_bar(
            lambda bar: self._on_bar_wrapper(bar, original_on_bar)
        )

        # Start the pipeline (connect, resolve contract, data feed, recon)
        started = await self._pipeline.start()
        if not started:
            logger.critical("Pipeline failed to start — exiting")
            sys.exit(1)

        self._session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._last_session = get_session_type(datetime.now(timezone.utc))
        self._log_decision("session_start", {
            "account_type": self._ibkr_config.account_type,
            "symbol": self._ibkr_config.symbol,
            "session": self._last_session.value,
            "dry_run": self._dry_run,
        })

        logger.info("Pipeline started — waiting for bars")

        # Main loop
        await self._run_loop()

    def _print_banner(self) -> None:
        logger.info("=" * 60)
        logger.info("  IBKR LIVE TRADING RUNNER")
        logger.info("  Gateway:       %s:%d",
                     self._ibkr_config.gateway_host,
                     self._ibkr_config.gateway_port)
        logger.info("  Account:       %s", self._ibkr_config.account_type)
        logger.info("  Symbol:        %s", self._ibkr_config.symbol)
        logger.info("  HC filter:     score>=0.75, stop<=30pts")
        logger.info("  HTF gate:      strength>=0.3")
        logger.info("  ETH trading:   %s",
                     "ON" if self._executor_config.allow_eth else "OFF")
        logger.info("  Paper mode:    %s",
                     "ON" if self._executor_config.paper_mode else "OFF")
        logger.info("  Dry run:       %s", "ON" if self._dry_run else "OFF")
        logger.info("=" * 60)

    # ──────────────────────────────────────────────────────────
    # BAR WRAPPER — warmup gating + logging
    # ──────────────────────────────────────────────────────────

    def _on_bar_wrapper(self, bar, original_on_bar) -> None:
        """
        Intercepts every bar for:
          1. Warmup tracking — count bars, suppress trading until primed
          2. Session transition detection
          3. Decision logging
          4. Dry-run mode (log only, no trading)
        """
        self._warmup_bar_count += 1

        # Detect RTH/ETH transitions
        current_session = get_session_type(bar.timestamp)
        if self._last_session and current_session != self._last_session:
            self._on_session_transition(self._last_session, current_session)
        self._last_session = current_session

        # Warmup phase — feed bars to prime indicators, but don't trade
        if not self._warmup_complete:
            if self._warmup_bar_count < self.WARMUP_BARS:
                # Feed through pipeline for indicator warmup only
                # The pipeline won't trade because we set it to
                # a non-RUNNING state during warmup
                self._pipeline._feature_engine.update(bar)
                if self._warmup_bar_count % 10 == 0:
                    logger.info(
                        "WARMUP: %d/%d bars",
                        self._warmup_bar_count,
                        self.WARMUP_BARS,
                    )
                return

            # Warmup complete
            self._warmup_complete = True
            logger.info("=" * 60)
            logger.info("  WARMUP COMPLETE — TRADING ACTIVE")
            logger.info("  Indicators primed with %d bars", self.WARMUP_BARS)
            logger.info("=" * 60)
            self._log_decision("warmup_complete", {
                "bars_used": self.WARMUP_BARS,
            })

        # Dry run — log the bar but don't execute
        if self._dry_run:
            self._log_decision("bar_dry_run", {
                "close": bar.close,
                "volume": bar.volume,
                "session": current_session.value,
            })
            return

        # Forward to pipeline for real processing
        original_on_bar(bar)

        # Log the decision outcome
        self._log_bar_decision(bar, current_session)

    def _log_bar_decision(self, bar, session: SessionType) -> None:
        """Log every bar's outcome to ibkr_decisions.json."""
        pipeline = self._pipeline
        status = pipeline.get_status()

        self._decision_log.log({
            "event": "bar_processed",
            "timestamp": bar.timestamp.isoformat(),
            "close": bar.close,
            "volume": bar.volume,
            "session": session.value,
            "regime": status["current_regime"],
            "htf_consensus": status["htf_consensus"],
            "active_group": status["active_group_id"],
            "executor_halted": status["executor"].get("is_halted", False),
            "bars_processed": pipeline.bars_processed,
        })

    # ──────────────────────────────────────────────────────────
    # SESSION MANAGEMENT
    # ──────────────────────────────────────────────────────────

    def _on_session_transition(
        self, old: SessionType, new: SessionType
    ) -> None:
        """Handle RTH ↔ ETH transitions."""
        logger.info(
            "SESSION TRANSITION: %s → %s", old.value, new.value
        )
        self._log_decision("session_transition", {
            "from": old.value,
            "to": new.value,
        })

        if new == SessionType.ETH and old == SessionType.RTH:
            # RTH ended — reset daily counters for new session
            self._on_daily_reset()

    def _on_daily_reset(self) -> None:
        """Reset daily PnL and counters at session boundary."""
        if not self._pipeline:
            return

        old_date = self._session_date
        self._session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Log daily summary before reset
        status = self._pipeline.get_status()
        self._log_decision("daily_reset", {
            "old_date": old_date,
            "new_date": self._session_date,
            "final_status": status,
        })

        self._pipeline._executor.reset_daily()
        self._pipeline._position_manager.reset_daily()
        logger.info("Daily reset complete — new session: %s", self._session_date)

    # ──────────────────────────────────────────────────────────
    # TERMINAL DASHBOARD
    # ──────────────────────────────────────────────────────────

    def _print_dashboard(self) -> None:
        """
        Print a status dashboard to terminal.
        Display only — no trading logic.
        """
        if not self._pipeline:
            return

        now_utc = datetime.now(timezone.utc)
        et_now = now_utc.astimezone(ET_TZ)
        session = get_session_type(now_utc)

        pipeline = self._pipeline
        executor = pipeline._executor
        pm = pipeline._position_manager
        bridge = pipeline._bridge

        exec_status = executor.get_status()
        pm_status = pm.get_status()

        # ── Position ──
        positions = pm.open_positions
        if positions:
            # Determine side + contracts from open positions
            pos_list = list(positions.values())
            side = pos_list[0].side.value  # "LONG" or "SHORT"
            total_contracts = sum(p.contracts for p in pos_list)
            avg_entry = sum(
                p.entry_price * p.contracts for p in pos_list
            ) / total_contracts
            pos_line = f"{side} {total_contracts}x @ {avg_entry:.2f}"
            tags = ", ".join(p.tag for p in pos_list if p.tag)
            pos_detail = f"Legs: {tags}" if tags else ""
        else:
            pos_line = "FLAT"
            pos_detail = "(waiting for signal)"

        # ── P&L ──
        realized = pm_status["daily_realized_pnl"]
        current_price = 0.0
        if pipeline._last_bar:
            current_price = pipeline._last_bar.close
        unrealized = pm.get_unrealized_pnl(current_price)
        net_pnl = realized + unrealized

        # ── Trades today — wins / losses ──
        trade_count = pm_status["trade_count"]
        wins = 0
        losses = 0
        for pos in pm._closed_positions:
            if pos.net_pnl > 0:
                wins += 1
            elif pos.net_pnl < 0:
                losses += 1

        # ── Filter blocks ──
        signal_stats = pipeline._signal_aggregator.get_signal_stats()
        htf_blocked = signal_stats.get("htf_blocked_signals", 0)
        bridge_rejected = bridge.rejections
        exec_blocked = exec_status["daily_blocked"]

        # ── Connection ──
        client_status = pipeline._client.get_status()
        gw_ok = client_status.get("connected", False)
        feed_status = pipeline._data_feed.get_status()
        feed_mode = feed_status.get("data_mode", "none")
        recon_ok = pm_status.get("recon_loop_active", False)
        halted = exec_status["is_halted"]
        halt_reason = exec_status["halt_reason"]

        # ── Time to next session boundary ──
        boundary_label, boundary_delta = self._next_session_boundary(et_now)

        # ── Render ──
        W = 62
        bar = "=" * W
        et_str = et_now.strftime("%H:%M:%S ET")

        lines = [
            "",
            bar,
            f"  IBKR DASHBOARD  {et_str:>20}      Session: {session.value}",
            bar,
            "",
            f"  POSITION    {pos_line}",
            f"              {pos_detail}",
            "",
            f"  DAILY P&L   Realized: ${realized:+.2f}"
            f"   Unrealized: ${unrealized:+.2f}",
            f"              Net: ${net_pnl:+.2f}",
            "",
            f"  TRADES      {trade_count} today"
            f" ({wins}W / {losses}L)",
            f"              Blocked:"
            f" {bridge_rejected} HC"
            f" | {htf_blocked} HTF"
            f" | {exec_blocked} executor",
            "",
            f"  CONNECTION  Gateway: {'OK' if gw_ok else 'DOWN'}"
            f"  Feed: {feed_mode}"
            f"  Recon: {'OK' if recon_ok else 'OFF'}",
            f"              Halted: "
            + (f"YES — {halt_reason}" if halted else "No"),
            "",
            f"  NEXT        {boundary_label} in {boundary_delta}",
            "",
            bar,
        ]

        print("\n".join(lines))

    @staticmethod
    def _next_session_boundary(et_now: datetime) -> tuple:
        """
        Compute the next RTH boundary from current ET time.

        Returns (label, "Xh Ym") tuple.

        Session boundaries (ET):
          RTH open:  09:30
          RTH close: 16:00
          ETH start: 18:00
        """
        h = et_now.hour
        m = et_now.minute
        t = h + m / 60.0

        if 9.5 <= t < 16.0:
            # Currently RTH → next boundary is RTH close at 16:00
            target = et_now.replace(
                hour=16, minute=0, second=0, microsecond=0
            )
            label = "RTH close"
        elif t < 9.5:
            # Before RTH open → next boundary is RTH open at 09:30
            target = et_now.replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            label = "RTH open"
        else:
            # After 16:00 (ETH evening) → next boundary is RTH open tomorrow
            target = (et_now + timedelta(days=1)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            label = "RTH open"

        delta = target - et_now
        total_minutes = int(delta.total_seconds() / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60

        if hours > 0:
            delta_str = f"{hours}h {minutes:02d}m"
        else:
            delta_str = f"{minutes}m"

        return label, delta_str

    # ──────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Spin until shutdown signal. Bars arrive via callbacks."""
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(1.0)

                if not self._pipeline:
                    break

                # Check if executor halted (kill switch / daily loss)
                if self._pipeline._executor.is_halted:
                    logger.warning(
                        "Executor HALTED: %s",
                        self._pipeline._executor._state.halt_reason,
                    )
                    self._log_decision("executor_halted", {
                        "reason": self._pipeline._executor._state.halt_reason,
                    })
                    break

                # Dashboard refresh every 2 minutes
                now = time.monotonic()
                if now - self._last_dashboard_time >= self.DASHBOARD_INTERVAL_SECONDS:
                    if self._warmup_complete:
                        self._print_dashboard()
                    self._last_dashboard_time = now

        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    # ──────────────────────────────────────────────────────────
    # TRADE EVENT HOOKS
    # ──────────────────────────────────────────────────────────

    def log_fill(self, fill_data: dict) -> None:
        """Log a fill event to ibkr_trades.json."""
        self._trade_log.log({
            "event": "fill",
            **fill_data,
        })

    # ──────────────────────────────────────────────────────────
    # DECISION LOGGING
    # ──────────────────────────────────────────────────────────

    def _log_decision(self, decision_type: str, data: dict) -> None:
        self._decision_log.log({
            "event": decision_type,
            **data,
        })

    # ──────────────────────────────────────────────────────────
    # SHUTDOWN — NEVER leave positions open
    # ──────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """
        Graceful shutdown sequence:
          1. Flatten all open positions
          2. Cancel all open orders
          3. Stop pipeline (recon loop, data feed, IBKR connection)
          4. Flush all logs
        """
        if not self._pipeline:
            return

        logger.info("=" * 60)
        logger.info("  SHUTDOWN INITIATED")
        logger.info("=" * 60)

        # 1. Flatten positions
        pm = self._pipeline._position_manager
        if pm.open_position_count > 0:
            logger.warning(
                "Flattening %d open positions before shutdown",
                pm.open_position_count,
            )
            last_price = 0.0
            if self._pipeline._last_bar:
                last_price = self._pipeline._last_bar.close

            if last_price > 0:
                for pos in list(pm.open_positions):
                    self._pipeline.close_position(
                        pos.position_id, last_price, "shutdown_flatten"
                    )
                    self._trade_log.log({
                        "event": "shutdown_flatten",
                        "position_id": pos.position_id,
                        "exit_price": last_price,
                    })

        # 2. Cancel open orders
        executor = self._pipeline._executor
        cancelled = await executor.cancel_all_open_orders()
        if cancelled > 0:
            logger.info("Cancelled %d open orders", cancelled)

        # 3. Log final status
        status = self._pipeline.get_status()
        self._log_decision("shutdown", {
            "bars_processed": self._pipeline.bars_processed,
            "final_status": status,
        })

        # 4. Stop pipeline
        await self._pipeline.stop()

        # 5. Flush logs
        self._trade_log.flush()
        self._decision_log.flush()

        logger.info("Shutdown complete — all positions flat, logs flushed")
        self._pipeline = None

    def request_shutdown(self) -> None:
        """Called from signal handler."""
        self._shutdown_event.set()


# ═══════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IBKR Live Trading Runner — MNQ via Client Portal Gateway"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Connect and process bars but don't execute trades",
    )
    parser.add_argument(
        "--allow-eth", action="store_true",
        help="Allow trading during Extended Trading Hours",
    )
    parser.add_argument(
        "--symbol", type=str, default=None,
        help="Override trading symbol (default: MNQ)",
    )
    args = parser.parse_args()

    # Validate environment
    env_vals = validate_env()

    # Create log directory
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Configure logging — console + file + error file
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        str(LOGS_DIR / "ibkr_trading.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        str(ERRORS_LOG),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s\n%(exc_info)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(error_handler)

    # Build IBKR config from env
    ibkr_config = IBKRConfig(
        gateway_host=env_vals["IBKR_GATEWAY_HOST"],
        gateway_port=int(env_vals["IBKR_GATEWAY_PORT"]),
        account_type=env_vals["IBKR_ACCOUNT_TYPE"],
        symbol=args.symbol or os.environ.get("IBKR_SYMBOL", "MNQ"),
    )

    # Safety: paper mode unless explicitly live
    executor_config = ExecutorConfig(
        allow_eth=args.allow_eth,
        paper_mode=(ibkr_config.account_type != "live"),
    )

    if ibkr_config.is_live:
        logger.warning("=" * 60)
        logger.warning("  *** LIVE TRADING MODE ***")
        logger.warning("  Real money at risk. Ctrl+C to abort.")
        logger.warning("=" * 60)

    runner = IBKRLiveRunner(
        ibkr_config=ibkr_config,
        executor_config=executor_config,
        dry_run=args.dry_run,
    )

    # Event loop with signal handlers
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("Shutdown signal received (SIGINT/SIGTERM)")
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

    # Shutdown timeout: if flatten/cancel hangs, force exit after 30s
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
                "Shutdown timed out after %ds — "
                "MANUAL POSITION CHECK REQUIRED",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
    except Exception:
        # NEVER leave positions open on crash
        logger.critical(
            "UNHANDLED EXCEPTION — flattening all positions\n%s",
            traceback.format_exc(),
        )
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            logger.critical(
                "Shutdown timed out after %ds — "
                "MANUAL POSITION CHECK REQUIRED",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
