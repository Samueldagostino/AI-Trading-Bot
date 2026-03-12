"""
TWS Health Monitor
==================
Async background task that monitors TWS connection health alongside
the trading loop. Writes heartbeat state to logs/heartbeat_state.json
for publish_stats.py to consume.

Checks every 10 seconds:
  - Socket alive?
  - Bars flowing?
  - TWS process running?

3 consecutive failures → trigger auto-relaunch callback.

SECURITY: No sensitive data in heartbeat file -- only timestamps and quality metrics.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class TWSHealthMonitor:
    """
    Monitors TWS connection health and writes heartbeat state for the website.
    """

    def __init__(
        self,
        client,
        launcher=None,
        heartbeat_file: Optional[Path] = None,
        check_interval: float = 10.0,
        failure_threshold: int = 3,
        on_critical_failure: Optional[Callable] = None,
    ):
        """
        Args:
            client: IBKRClient instance (must have is_connected() and _last_heartbeat).
            launcher: TWSLauncher instance (optional, for process-level checks).
            heartbeat_file: Path to write heartbeat_state.json.
            check_interval: Seconds between health checks.
            failure_threshold: Consecutive failures before triggering restart.
            on_critical_failure: Callback when threshold is reached.
        """
        self._client = client
        self._launcher = launcher
        self._heartbeat_file = heartbeat_file
        self._check_interval = check_interval
        self._failure_threshold = failure_threshold
        self._on_critical_failure = on_critical_failure

        # State tracking
        self._consecutive_failures = 0
        self._total_checks = 0
        self._total_ok = 0
        self._uptime_start = time.monotonic()
        self._last_bar_time: Optional[float] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ──────────────────────────────────────────────────────
    # LIFECYCLE
    # ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the health monitor as an async background task."""
        if self._running:
            logger.warning("Health monitor already running")
            return

        self._running = True
        self._uptime_start = time.monotonic()
        self._task = asyncio.ensure_future(self._monitor_loop())
        logger.info(
            "Health monitor started (interval=%.1fs, threshold=%d)",
            self._check_interval,
            self._failure_threshold,
        )

    def stop(self) -> None:
        """Stop the health monitor."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Health monitor stopped")

    # ──────────────────────────────────────────────────────
    # MONITOR LOOP
    # ──────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        try:
            while self._running:
                await asyncio.sleep(self._check_interval)
                self._run_health_check()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Health monitor crashed: %s", e)
        finally:
            self._running = False

    def _run_health_check(self) -> None:
        """Run a single health check cycle."""
        self._total_checks += 1

        checks = {
            "socket_alive": self._check_socket(),
            "bars_flowing": self._check_bars(),
            "process_alive": self._check_process(),
        }

        all_ok = all(checks.values())

        if all_ok:
            self._consecutive_failures = 0
            self._total_ok += 1
            quality = "good"
            logger.debug(
                "Health OK: socket=%s bars=%s process=%s",
                checks["socket_alive"],
                checks["bars_flowing"],
                checks["process_alive"],
            )
        else:
            self._consecutive_failures += 1
            failed = [k for k, v in checks.items() if not v]
            quality = "fair" if self._consecutive_failures < self._failure_threshold else "poor"
            logger.warning(
                "Health FAIL (%d/%d): %s",
                self._consecutive_failures,
                self._failure_threshold,
                ", ".join(failed),
            )

            # Trigger restart if threshold reached
            if self._consecutive_failures >= self._failure_threshold:
                logger.critical(
                    "Health threshold breached (%d consecutive failures) -- triggering recovery",
                    self._consecutive_failures,
                )
                if self._on_critical_failure:
                    try:
                        self._on_critical_failure()
                    except Exception as e:
                        logger.error("Recovery callback failed: %s", e)

        # Write heartbeat state
        self._write_heartbeat(quality, checks)

    # ──────────────────────────────────────────────────────
    # INDIVIDUAL CHECKS
    # ──────────────────────────────────────────────────────

    def _check_socket(self) -> bool:
        """Check if IB API connection is alive."""
        try:
            return self._client.is_connected()
        except Exception:
            return False

    def _check_bars(self) -> bool:
        """Check if bars are flowing (last bar within 60 seconds)."""
        try:
            last_hb = getattr(self._client, "_last_heartbeat", 0.0)
            if last_hb <= 0:
                return False
            age = time.monotonic() - last_hb
            return age < 60.0  # Bars should arrive every 5 seconds
        except Exception:
            return False

    def _check_process(self) -> bool:
        """Check if TWS process is still running."""
        if self._launcher is None:
            return True  # No launcher = skip process check
        try:
            return self._launcher.is_running()
        except Exception:
            return False

    # ──────────────────────────────────────────────────────
    # HEARTBEAT FILE
    # ──────────────────────────────────────────────────────

    def _write_heartbeat(self, quality: str, checks: dict) -> None:
        """Write heartbeat state to JSON file for publish_stats.py."""
        if not self._heartbeat_file:
            return

        now = datetime.now(timezone.utc)
        uptime_total = time.monotonic() - self._uptime_start
        uptime_pct = (self._total_ok / self._total_checks * 100) if self._total_checks > 0 else 0.0

        state = {
            "last_seen": now.isoformat(),
            "connection_quality": quality,
            "uptime_pct": round(uptime_pct, 1),
            "consecutive_failures": self._consecutive_failures,
            "total_checks": self._total_checks,
            "total_ok": self._total_ok,
            "uptime_seconds": round(uptime_total, 0),
            "checks": checks,
        }

        try:
            self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._heartbeat_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(str(tmp), str(self._heartbeat_file))
        except OSError as e:
            logger.warning("Failed to write heartbeat: %s", e)

    # ──────────────────────────────────────────────────────
    # PUBLIC STATE
    # ──────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Get current health state as a dict (for programmatic access)."""
        uptime_total = time.monotonic() - self._uptime_start
        uptime_pct = (self._total_ok / self._total_checks * 100) if self._total_checks > 0 else 0.0

        return {
            "running": self._running,
            "consecutive_failures": self._consecutive_failures,
            "total_checks": self._total_checks,
            "total_ok": self._total_ok,
            "uptime_pct": round(uptime_pct, 1),
            "uptime_seconds": round(uptime_total, 0),
            "quality": "good" if self._consecutive_failures == 0
                       else "fair" if self._consecutive_failures < self._failure_threshold
                       else "poor",
        }

    @property
    def is_healthy(self) -> bool:
        """Quick health check -- True if no consecutive failures."""
        return self._consecutive_failures == 0

    @property
    def is_critical(self) -> bool:
        """True if consecutive failures have exceeded threshold."""
        return self._consecutive_failures >= self._failure_threshold
