"""
TWS Process Launcher
====================
Detects TWS installation, launches via IBC for automated login,
and waits for TWS to be ready by probing the API socket port.

Supports:
  - Auto-detect TWS on Windows (common Jts paths)
  - Launch via IBC (handles login dialog, 2FA, paper-trading warning)
  - Direct launch fallback (user must log in manually)
  - Port probing with configurable timeout
  - Process lifecycle management (PID tracking, kill)

SECURITY: Credentials are passed to IBC via config file, never logged.

Usage:
    launcher = TWSLauncher(config)
    launcher.launch()
    launcher.wait_for_ready(timeout=120)
    ...
    launcher.kill()
"""

import logging
import os
import platform
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TWSLauncher:
    """
    Manages TWS process lifecycle: launch, readiness check, and shutdown.
    """

    def __init__(self, config):
        """
        Args:
            config: TWSAutoConfig instance with all paths and settings.
        """
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._pid: Optional[int] = None
        self._launch_time: Optional[float] = None
        self._ibc_ini_path: Optional[Path] = None

    # ──────────────────────────────────────────────────────
    # LAUNCH
    # ──────────────────────────────────────────────────────

    def launch(self) -> bool:
        """
        Launch TWS, preferring IBC for automated login.

        Returns:
            True if process started (does NOT mean TWS is ready yet).
        """
        if self.is_running():
            logger.info("TWS is already running (PID %d)", self._pid)
            return True

        if self._config.ibc_available:
            return self._launch_via_ibc()
        elif self._config.tws_available:
            return self._launch_direct()
        else:
            logger.error(
                "TWS not found. Checked paths:\n"
                "  TWS: %s\n"
                "  IBC: %s\n"
                "Please install TWS and/or IBC, or set IBKR_TWS_PATH / IBKR_IBC_PATH in .env",
                self._config.tws_path or "(not set)",
                self._config.ibc_path or "(not set)",
            )
            return False

    def _launch_via_ibc(self) -> bool:
        """Launch TWS through IBC for automated login."""
        logger.info("Launching TWS via IBC at %s", self._config.ibc_path)

        # Generate IBC config with credentials
        ibc_dir = Path(self._config.ibc_path)
        self._ibc_ini_path = ibc_dir / "config.ini"

        try:
            self._config.generate_ibc_ini(self._ibc_ini_path)
        except Exception as e:
            logger.error("Failed to generate IBC config: %s", e)
            return False

        # Build IBC launch command
        start_script = ibc_dir / "StartTWS.bat"
        if not start_script.exists():
            # Try Scripts subfolder (some IBC installations)
            start_script = ibc_dir / "Scripts" / "StartTWS.bat"

        if not start_script.exists():
            logger.error("IBC StartTWS.bat not found in %s", ibc_dir)
            return False

        cmd = [
            str(start_script),
            str(self._ibc_ini_path),
        ]

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(ibc_dir),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if platform.system() == "Windows"
                else 0,
            )
            self._pid = self._process.pid
            self._launch_time = time.monotonic()
            logger.info("IBC started (PID %d), waiting for TWS to initialize...", self._pid)
            return True
        except Exception as e:
            logger.error("Failed to launch IBC: %s", e)
            return False

    def _launch_direct(self) -> bool:
        """Launch TWS directly (user must log in manually)."""
        logger.warning(
            "IBC not available — launching TWS directly. "
            "You will need to log in manually within %ds.",
            self._config.startup_timeout,
        )

        tws_exe = Path(self._config.tws_path)

        try:
            self._process = subprocess.Popen(
                [str(tws_exe)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if platform.system() == "Windows"
                else 0,
            )
            self._pid = self._process.pid
            self._launch_time = time.monotonic()
            logger.info("TWS launched directly (PID %d)", self._pid)
            return True
        except Exception as e:
            logger.error("Failed to launch TWS: %s", e)
            return False

    # ──────────────────────────────────────────────────────
    # READINESS
    # ──────────────────────────────────────────────────────

    def wait_for_ready(self, timeout: Optional[int] = None) -> bool:
        """
        Block until TWS API port is accepting connections.

        Args:
            timeout: Max seconds to wait (default: config.startup_timeout).

        Returns:
            True if TWS is ready, False if timed out.
        """
        timeout = timeout or self._config.startup_timeout
        interval = self._config.port_probe_interval
        deadline = time.monotonic() + timeout

        logger.info(
            "Waiting for TWS API on %s:%d (timeout=%ds)...",
            self._config.tws_host,
            self._config.tws_port,
            timeout,
        )

        while time.monotonic() < deadline:
            if self._probe_port():
                elapsed = time.monotonic() - (self._launch_time or time.monotonic())
                logger.info("TWS API port is open (took %.1fs)", elapsed)

                # Extra delay for TWS internal initialization
                if self._config.post_login_delay > 0:
                    logger.info(
                        "Waiting %ds for TWS internal warm-up...",
                        self._config.post_login_delay,
                    )
                    time.sleep(self._config.post_login_delay)

                return True

            # Check if process died
            if self._process and self._process.poll() is not None:
                logger.error(
                    "TWS/IBC process exited prematurely (exit code %d)",
                    self._process.returncode,
                )
                self._read_process_output()
                return False

            time.sleep(interval)

        logger.error("TWS did not become ready within %ds", timeout)
        return False

    def _probe_port(self) -> bool:
        """Check if TWS API port is accepting TCP connections."""
        try:
            with socket.create_connection(
                (self._config.tws_host, self._config.tws_port),
                timeout=2.0,
            ):
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            return False

    # ──────────────────────────────────────────────────────
    # PROCESS MANAGEMENT
    # ──────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """Check if the TWS process is still alive."""
        if self._process is None:
            return self._check_port_alive()

        poll = self._process.poll()
        if poll is not None:
            return False

        return True

    def _check_port_alive(self) -> bool:
        """Fallback: check if port is accepting connections (TWS may have been started externally)."""
        return self._probe_port()

    def kill(self) -> None:
        """Terminate the TWS process."""
        if self._process is None:
            logger.info("No TWS process to kill")
            return

        logger.info("Killing TWS process (PID %d)...", self._pid or 0)

        try:
            if platform.system() == "Windows":
                # Windows: use taskkill for reliable termination
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(self._pid), "/T"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()

            logger.info("TWS process terminated")
        except Exception as e:
            logger.error("Failed to kill TWS: %s", e)
        finally:
            self._process = None
            self._pid = None

    def get_pid(self) -> Optional[int]:
        """Return the TWS process PID, if known."""
        return self._pid

    def uptime(self) -> float:
        """Return seconds since launch, or 0 if not launched."""
        if self._launch_time is None:
            return 0.0
        return time.monotonic() - self._launch_time

    # ──────────────────────────────────────────────────────
    # CLEANUP
    # ──────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Clean up temporary files (IBC config with credentials)."""
        if self._ibc_ini_path and self._ibc_ini_path.exists():
            try:
                self._ibc_ini_path.unlink()
                logger.debug("Cleaned up IBC config at %s", self._ibc_ini_path)
            except OSError as e:
                logger.warning("Failed to clean up IBC config: %s", e)

    def _read_process_output(self) -> None:
        """Read and log any output from the terminated process."""
        if self._process is None:
            return
        try:
            stdout, stderr = self._process.communicate(timeout=5)
            if stdout:
                logger.info("TWS stdout: %s", stdout.decode(errors="replace")[:500])
            if stderr:
                logger.warning("TWS stderr: %s", stderr.decode(errors="replace")[:500])
        except Exception:
            pass
