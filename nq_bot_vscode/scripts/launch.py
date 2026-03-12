"""
NQ Trading Bot — Unified Launcher
===================================
One command to rule them all:

  1. Backup to Google Drive
  2. Launch TWS (auto-login via IBC if available)
  3. Wait for TWS to be ready
  4. Start the trading bot (run_paper_live.py)
  5. Start the website data feeder (publish_stats.py)
  6. Graceful shutdown on Ctrl+C (flatten positions, stop all)

Usage:
    python scripts/launch.py
    python scripts/launch.py --dry-run
    python scripts/launch.py --skip-backup
    python scripts/launch.py --port 7497 --no-tws-launch

One terminal. Everything integrated.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
ROOT_DIR = PROJECT_DIR.parent
LOGS_DIR = PROJECT_DIR / "logs"

logger = logging.getLogger("launch")


# ═══════════════════════════════════════════════════════════════
# STEP 1: Google Drive Backup
# ═══════════════════════════════════════════════════════════════

def run_gdrive_backup() -> bool:
    """Run Google Drive sync script. Returns True on success."""
    sync_script = SCRIPT_DIR / "sync_to_gdrive.ps1"
    if sys.platform != "win32" or not sync_script.exists():
        logger.info("Google Drive backup: skipped (not Windows or script not found)")
        return True

    logger.info("=" * 50)
    logger.info("  STEP 1: Google Drive Backup")
    logger.info("=" * 50)

    try:
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(sync_script)],
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("  Backup complete")
            return True
        else:
            logger.warning("  Backup had warnings (continuing)")
            return True
    except subprocess.TimeoutExpired:
        logger.warning("  Backup timed out after 120s (continuing)")
        return True
    except Exception as e:
        logger.warning("  Backup failed: %s (continuing)", e)
        return True


# ═══════════════════════════════════════════════════════════════
# STEP 2: Launch TWS
# ═══════════════════════════════════════════════════════════════

def check_tws_port(port: int = 7497, timeout: float = 2.0) -> bool:
    """Quick check if TWS API port is accepting connections."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def launch_tws(port: int = 7497, wait_timeout: int = 120) -> bool:
    """
    Launch TWS via IBC (auto-login) or direct launch.
    Waits for the API port to be ready.
    """
    logger.info("=" * 50)
    logger.info("  STEP 2: Launch TWS")
    logger.info("=" * 50)

    # Check if already running
    if check_tws_port(port):
        logger.info("  TWS already running on port %d", port)
        return True

    # Try to use TWSLauncher
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from config.tws_auto_config import TWSAutoConfig
        from Broker.tws_launcher import TWSLauncher

        config = TWSAutoConfig()
        launcher = TWSLauncher(config)

        if config.ibc_available:
            logger.info("  Launching TWS via IBC (auto-login)...")
        elif config.tws_available:
            logger.info("  Launching TWS directly (manual login required)...")
        else:
            logger.warning("  TWS/IBC not found — please start TWS manually")
            logger.info("  Waiting up to %ds for TWS on port %d...", wait_timeout, port)
            return _wait_for_port(port, wait_timeout)

        if not launcher.launch():
            logger.error("  Failed to launch TWS process")
            return False

        logger.info("  TWS process started — waiting for API port %d...", port)
        return _wait_for_port(port, wait_timeout)

    except ImportError as e:
        logger.warning("  TWSLauncher not available (%s)", e)
        logger.info("  Please start TWS manually. Waiting up to %ds...", wait_timeout)
        return _wait_for_port(port, wait_timeout)


def _wait_for_port(port: int, timeout: int) -> bool:
    """Wait for TWS API port to accept connections."""
    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        if check_tws_port(port):
            logger.info("  TWS ready on port %d (took %.0fs)", port, time.time() - start)
            return True
        time.sleep(3)
        dots += 1
        if dots % 10 == 0:
            elapsed = time.time() - start
            logger.info("  Still waiting... (%.0fs / %ds)", elapsed, timeout)

    logger.error("  TWS did not respond on port %d after %ds", port, timeout)
    return False


# ═══════════════════════════════════════════════════════════════
# STEP 3: Start Trading Bot
# ═══════════════════════════════════════════════════════════════

def start_bot(port: int, max_daily_loss: float, log_level: str,
              dry_run: bool) -> subprocess.Popen:
    """Start run_paper_live.py as a subprocess."""
    logger.info("=" * 50)
    logger.info("  STEP 3: Start Trading Bot")
    logger.info("=" * 50)

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "run_paper_live.py"),
        "--port", str(port),
        "--max-daily-loss", str(max_daily_loss),
        "--log-level", log_level,
    ]
    if dry_run:
        cmd.append("--dry-run")

    logger.info("  Command: %s", " ".join(cmd))

    # Run bot in foreground (inherit stdio so you see all output)
    # But we need it as a process we can manage
    bot_proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
    )
    logger.info("  Bot started (PID %d)", bot_proc.pid)
    return bot_proc


# ═══════════════════════════════════════════════════════════════
# STEP 4: Start Website Data Feeder
# ═══════════════════════════════════════════════════════════════

def start_publisher(interval: int = 60) -> subprocess.Popen:
    """Start publish_stats.py as a background subprocess."""
    logger.info("=" * 50)
    logger.info("  STEP 4: Start Website Data Feeder")
    logger.info("=" * 50)

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "publish_stats.py"),
        "--interval", str(interval),
    ]

    # Run publisher in background with output going to a log file
    pub_log = LOGS_DIR / "publisher.log"
    pub_log_handle = open(pub_log, "a", encoding="utf-8")

    pub_proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=pub_log_handle,
        stderr=pub_log_handle,
    )
    logger.info("  Publisher started (PID %d)", pub_proc.pid)
    logger.info("  Publishing to website every %ds", interval)
    logger.info("  Publisher log: %s", pub_log)
    return pub_proc


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="NQ Trading Bot — Unified Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/launch.py                     # Full startup
    python scripts/launch.py --dry-run           # Synthetic data, no TWS
    python scripts/launch.py --skip-backup       # Skip Google Drive backup
    python scripts/launch.py --no-tws-launch     # Don't auto-launch TWS (already open)
    python scripts/launch.py --publish-interval 30  # Publish stats every 30s
        """,
    )
    parser.add_argument("--port", type=int, default=7497,
                        help="TWS API port (default: 7497)")
    parser.add_argument("--max-daily-loss", type=float, default=1500.0,
                        help="Max daily loss (default: $1500)")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Run bot with synthetic data (no TWS)")
    parser.add_argument("--skip-backup", action="store_true",
                        help="Skip Google Drive backup on startup")
    parser.add_argument("--no-tws-launch", action="store_true",
                        help="Don't try to launch TWS (assume it's running)")
    parser.add_argument("--publish-interval", type=int, default=60,
                        help="Website publish interval in seconds (default: 60)")
    parser.add_argument("--tws-wait", type=int, default=120,
                        help="Max seconds to wait for TWS (default: 120)")
    args = parser.parse_args()

    # ── Logging ──
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Ensure logs directory exists
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print()
    print("=" * 60)
    print("  NQ TRADING SYSTEM — UNIFIED LAUNCHER")
    print(f"  {timestamp}")
    print("=" * 60)
    print(f"  TWS Port:        {args.port}")
    print(f"  Max Daily Loss:  ${args.max_daily_loss:.0f}")
    print(f"  Mode:            {'DRY-RUN' if args.dry_run else 'LIVE DATA'}")
    print(f"  Website Publish: Every {args.publish_interval}s")
    print(f"  Google Drive:    {'Skip' if args.skip_backup else 'Backup on startup'}")
    print("=" * 60)
    print()

    bot_proc = None
    pub_proc = None

    try:
        # ── Step 1: Google Drive Backup ──
        if not args.skip_backup:
            run_gdrive_backup()
        else:
            logger.info("Google Drive backup: skipped (--skip-backup)")

        # ── Step 2: Launch TWS ──
        if not args.dry_run and not args.no_tws_launch:
            tws_ready = launch_tws(port=args.port, wait_timeout=args.tws_wait)
            if not tws_ready:
                logger.error("TWS not available — exiting")
                logger.error("Start TWS manually, or use --dry-run for testing")
                sys.exit(1)
        elif args.no_tws_launch:
            logger.info("TWS launch: skipped (--no-tws-launch)")
            if not check_tws_port(args.port):
                logger.warning("TWS not detected on port %d — bot may fail to connect", args.port)

        # ── Step 3: Start Website Publisher (background) ──
        pub_proc = start_publisher(interval=args.publish_interval)

        # ── Step 4: Start Trading Bot (foreground) ──
        bot_proc = start_bot(
            port=args.port,
            max_daily_loss=args.max_daily_loss,
            log_level=args.log_level,
            dry_run=args.dry_run,
        )

        print()
        print("=" * 60)
        print("  ALL SYSTEMS RUNNING")
        print(f"  Bot PID:       {bot_proc.pid}")
        print(f"  Publisher PID: {pub_proc.pid}")
        print(f"  Website:       makemoneymarkets.com (updates every {args.publish_interval}s)")
        print("  Press Ctrl+C to stop everything")
        print("=" * 60)
        print()

        # Wait for bot to exit (it runs until Ctrl+C or crash)
        bot_proc.wait()

    except KeyboardInterrupt:
        print()
        logger.info("Shutdown signal received — stopping all processes...")

    finally:
        # Stop bot
        if bot_proc and bot_proc.poll() is None:
            logger.info("Stopping bot (PID %d)...", bot_proc.pid)
            if sys.platform == "win32":
                bot_proc.send_signal(signal.CTRL_C_EVENT)
            else:
                bot_proc.send_signal(signal.SIGINT)
            try:
                bot_proc.wait(timeout=15)
                logger.info("Bot stopped gracefully")
            except subprocess.TimeoutExpired:
                bot_proc.terminate()
                bot_proc.wait(timeout=5)
                logger.warning("Bot force-terminated")

        # Stop publisher
        if pub_proc and pub_proc.poll() is None:
            logger.info("Stopping publisher (PID %d)...", pub_proc.pid)
            pub_proc.terminate()
            try:
                pub_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pub_proc.kill()
            logger.info("Publisher stopped")

        print()
        print("=" * 60)
        print("  ALL SYSTEMS STOPPED")
        print("=" * 60)
        print()


if __name__ == "__main__":
    main()
