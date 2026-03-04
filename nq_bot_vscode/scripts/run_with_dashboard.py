"""
Run Paper Trading Bot + Live Dashboard Together
=================================================
Starts the dashboard server on port 8080 in a background thread,
then launches run_paper_live.py as the main process.

Usage:
    python scripts/run_with_dashboard.py --dry-run
    python scripts/run_with_dashboard.py --port 7497
    python scripts/run_with_dashboard.py --dry-run --dashboard-port 9090
"""

import argparse
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent


def main():
    parser = argparse.ArgumentParser(
        description="Run paper trading bot with live dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Run bot in dry-run mode (synthetic data)")
    parser.add_argument("--port", type=int, default=7497, help="TWS/Gateway port for bot (default: 7497)")
    parser.add_argument("--max-daily-loss", type=float, default=500.0, help="Max daily loss (default: $500)")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    parser.add_argument("--dashboard-port", type=int, default=8080, help="Dashboard HTTP port (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Start dashboard server in background thread
    from scripts.live_dashboard import DashboardServer

    dashboard = DashboardServer(port=args.dashboard_port)
    dashboard.start(blocking=False)
    print(f"\n  Dashboard: http://localhost:{args.dashboard_port}")

    # Open browser after a short delay
    if not args.no_browser:
        def _open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{args.dashboard_port}")
        threading.Thread(target=_open_browser, daemon=True).start()

    # Build bot command
    bot_cmd = [
        sys.executable,
        str(SCRIPT_DIR / "run_paper_live.py"),
        "--log-level", args.log_level,
        "--port", str(args.port),
        "--max-daily-loss", str(args.max_daily_loss),
    ]
    if args.dry_run:
        bot_cmd.append("--dry-run")

    print(f"  Bot command: {' '.join(bot_cmd)}")
    print("  Press Ctrl+C to stop both\n")

    bot_process = None
    try:
        bot_process = subprocess.Popen(bot_cmd)
        bot_process.wait()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        if bot_process and bot_process.poll() is None:
            bot_process.send_signal(signal.SIGINT)
            try:
                bot_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                bot_process.terminate()
                bot_process.wait(timeout=5)
    finally:
        dashboard.stop()
        if bot_process and bot_process.poll() is None:
            bot_process.terminate()
        print("  All processes stopped.")


if __name__ == "__main__":
    main()
