#!/usr/bin/env python3
"""
Execution Report CLI
=====================
Generate execution analytics reports from the command line.

Usage:
    # Daily report
    python scripts/execution_report.py --daily 2026-03-06

    # Weekly report
    python scripts/execution_report.py --weekly 2026-03-03

    # Scaling readiness assessment
    python scripts/execution_report.py --scaling-readiness

    # Export raw data
    python scripts/execution_report.py --export-csv execution_data.csv
"""

import argparse
import asyncio
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent / "nq_bot_vscode"
sys.path.insert(0, str(project_root))

from monitoring.execution_analytics import ExecutionAnalytics
from monitoring.execution_report import ExecutionReport


async def main():
    parser = argparse.ArgumentParser(description="Execution Analytics Report Generator")
    parser.add_argument(
        "--daily", type=str, default=None,
        help="Generate daily report for date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--weekly", type=str, default=None,
        help="Generate weekly report starting from date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--scaling-readiness", action="store_true",
        help="Generate scaling readiness assessment",
    )
    parser.add_argument(
        "--export-csv", type=str, default=None,
        help="Export raw data to CSV file",
    )
    parser.add_argument(
        "--output-dir", type=str, default="docs",
        help="Output directory for HTML reports (default: docs/)",
    )
    parser.add_argument(
        "--db-host", type=str, default=os.getenv("DB_HOST", "localhost"),
        help="Database host",
    )
    parser.add_argument(
        "--db-port", type=int, default=int(os.getenv("DB_PORT", "5432")),
        help="Database port",
    )
    parser.add_argument(
        "--db-name", type=str, default=os.getenv("DB_NAME", "nq_bot"),
        help="Database name",
    )

    args = parser.parse_args()

    # Try to connect to DB, fall back to in-memory only
    analytics = ExecutionAnalytics()
    db_manager = None

    try:
        from database.connection import DatabaseManager
        conn_params = {
            "host": args.db_host,
            "port": args.db_port,
            "database": args.db_name,
            "user": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD", ""),
        }
        db_manager = DatabaseManager(conn_params)
        await db_manager.initialize()
        analytics = ExecutionAnalytics(db_manager=db_manager)
        loaded = await analytics.load_from_db(days=60)
        print(f"Loaded {loaded} execution records from database")
    except Exception as e:
        print(f"Warning: Could not connect to database ({e}). Using in-memory data only.")

    report = ExecutionReport(analytics)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.daily:
            target = date.fromisoformat(args.daily)
            html = report.generate_daily(target)
            outpath = output_dir / f"execution_daily_{target}.html"
            outpath.write_text(html, encoding="utf-8")
            print(f"Daily report written to {outpath}")

        elif args.weekly:
            start = date.fromisoformat(args.weekly)
            html = report.generate_weekly(start)
            outpath = output_dir / f"execution_weekly_{start}.html"
            outpath.write_text(html, encoding="utf-8")
            print(f"Weekly report written to {outpath}")

        elif args.scaling_readiness:
            html = report.generate_scaling_readiness()
            outpath = output_dir / "execution_scaling_readiness.html"
            outpath.write_text(html, encoding="utf-8")
            print(f"Scaling readiness report written to {outpath}")

        elif args.export_csv:
            count = analytics.export_csv(args.export_csv)
            print(f"Exported {count} records to {args.export_csv}")

        else:
            parser.print_help()

    finally:
        if db_manager:
            await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
