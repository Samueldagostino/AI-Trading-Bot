#!/usr/bin/env python3
"""
Paper-to-Live Comparison Report Generator
===========================================
Generates an HTML comparison report from a comparison log JSON.

Usage:
    python scripts/comparison_report.py \
        --log logs/comparison_log.json \
        --output reports/comparison.html
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("comparison_report")


def load_log(path: str) -> dict:
    """Load comparison log from JSON."""
    p = Path(path)
    if not p.exists():
        logger.error("Log file not found: %s", p)
        sys.exit(1)
    with open(p, "r") as f:
        return json.load(f)


def generate_report(data: dict, output_path: str) -> None:
    """Generate HTML comparison report."""
    summary = data.get("summary", {})
    verdict = data.get("verdict", {})
    comparisons = data.get("comparisons", [])
    fill_comparisons = data.get("fill_comparisons", [])
    divergences = data.get("divergences", [])

    verdict_val = verdict.get("verdict", "UNKNOWN")
    verdict_color = "#00ff88" if verdict_val == "PASS" else "#ff4444"

    # Build trade comparison table rows
    trade_rows = ""
    entries = [c for c in comparisons if c.get("paper_entry") or c.get("live_entry")]
    for c in entries:
        match = "match" if c.get("is_clean") else "DIVERGE"
        match_cls = "match" if c.get("is_clean") else "diverge"
        trade_rows += f"""
        <tr class="{match_cls}">
            <td>{c.get('bar_timestamp', '')[:19]}</td>
            <td>{c.get('bar_index', '')}</td>
            <td>{c.get('paper_direction', '-')}</td>
            <td>{c.get('live_direction', '-')}</td>
            <td>{c.get('paper_score', 0):.3f}</td>
            <td>{c.get('live_score', 0):.3f}</td>
            <td>{'Y' if c.get('paper_entry') else 'N'}</td>
            <td>{'Y' if c.get('live_entry') else 'N'}</td>
            <td class="{match_cls}">{match}</td>
        </tr>"""

    if not trade_rows:
        trade_rows = '<tr><td colspan="9" style="text-align:center">No trade signals in this session</td></tr>'

    # Fill comparison rows
    fill_rows = ""
    for fc in fill_comparisons:
        fill_rows += f"""
        <tr>
            <td>{fc.get('trade_id', '')}</td>
            <td>{fc.get('direction', '')}</td>
            <td>{fc.get('paper_entry_price', 0):.2f}</td>
            <td>{fc.get('live_entry_price', 0):.2f}</td>
            <td>{fc.get('entry_slippage_pts', 0):.3f}</td>
            <td>{fc.get('fill_latency_ms', 0):.0f}ms</td>
            <td>{'Y' if fc.get('stop_match') else 'N'}</td>
            <td>${fc.get('paper_pnl', 0):.2f}</td>
            <td>${fc.get('live_pnl', 0):.2f}</td>
            <td>${fc.get('pnl_delta', 0):.2f}</td>
        </tr>"""

    if not fill_rows:
        fill_rows = '<tr><td colspan="10" style="text-align:center">No fills to compare</td></tr>'

    # Divergence timeline rows
    div_rows = ""
    for d in divergences:
        sev_cls = d.get("severity", "INFO").lower()
        div_rows += f"""
        <tr class="{sev_cls}">
            <td>{d.get('timestamp', '')[:19]}</td>
            <td>{d.get('bar_index', '')}</td>
            <td class="{sev_cls}">{d.get('severity', '')}</td>
            <td>{d.get('category', '')}</td>
            <td>{d.get('description', '')}</td>
            <td>{d.get('paper_value', '')}</td>
            <td>{d.get('live_value', '')}</td>
        </tr>"""

    if not div_rows:
        div_rows = '<tr><td colspan="7" style="text-align:center; color:#00ff88">No divergences detected</td></tr>'

    # Checks detail
    checks = verdict.get("checks", {})
    checks_html = ""
    for check_name, passed in checks.items():
        icon = "PASS" if passed else "FAIL"
        color = "#00ff88" if passed else "#ff4444"
        checks_html += f'<div style="color:{color}; margin:4px 0">[{icon}] {check_name}</div>'

    # Fail reasons
    reasons_html = ""
    for reason in verdict.get("fail_reasons", []):
        reasons_html += f'<div style="color:#ff4444; margin:2px 0">  {reason}</div>'

    by_category = summary.get("by_category", {})
    cat_detail = ""
    for cat, count in by_category.items():
        cat_detail += f'<div style="margin:2px 0">  {cat}: {count}</div>'
    if not cat_detail:
        cat_detail = '<div style="color:#00ff88">  None</div>'

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Paper-to-Live Comparison Report</title>
    <style>
        body {{
            font-family: 'Courier New', monospace;
            background: #0a0a1a;
            color: #c0c0d0;
            padding: 20px;
            max-width: 1400px;
            margin: 0 auto;
        }}
        h1 {{ color: #00d4ff; border-bottom: 2px solid #1a3a5c; padding-bottom: 10px; }}
        h2 {{ color: #00aacc; margin-top: 30px; }}
        .verdict-box {{
            background: #0f1528;
            border: 3px solid {verdict_color};
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            text-align: center;
        }}
        .verdict-text {{
            font-size: 2.5em;
            color: {verdict_color};
            font-weight: bold;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 15px;
            margin: 20px 0;
        }}
        .summary-card {{
            background: #0f1528;
            border: 1px solid #1a3a5c;
            border-radius: 6px;
            padding: 15px;
        }}
        .summary-card .label {{ color: #6688aa; font-size: 0.9em; }}
        .summary-card .value {{ color: #00d4ff; font-size: 1.8em; font-weight: bold; }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 10px 0 20px 0;
        }}
        th {{
            background: #0f1f3c;
            color: #00d4ff;
            padding: 8px 6px;
            text-align: center;
            border: 1px solid #1a3a5c;
            font-size: 0.85em;
        }}
        td {{
            padding: 6px;
            text-align: center;
            border: 1px solid #1a2a3c;
            font-size: 0.85em;
        }}
        tr:nth-child(even) {{ background: #0a0f1e; }}
        tr.diverge {{ background: #2a0a0a; }}
        tr.match {{ }}
        td.diverge {{ color: #ff4444; font-weight: bold; }}
        td.match {{ color: #00ff88; }}
        tr.critical {{ background: #2a0a0a; }}
        tr.warning {{ background: #2a1a0a; }}
        tr.info {{ }}
        td.critical {{ color: #ff4444; font-weight: bold; }}
        td.warning {{ color: #ffaa00; }}
        td.info {{ color: #00aacc; }}
        .checks {{ background: #0f1528; padding: 15px; border-radius: 6px; font-family: monospace; }}
    </style>
</head>
<body>
    <h1>Paper-to-Live Comparison Report</h1>

    <div class="verdict-box">
        <div class="verdict-text">{verdict_val}</div>
        <div style="color:#6688aa; margin-top:8px">
            {"Safe to scale up contract size" if verdict_val == "PASS" else "Investigate divergences before scaling"}
        </div>
    </div>

    <div class="summary-grid">
        <div class="summary-card">
            <div class="label">Bars Compared</div>
            <div class="value">{summary.get('bars_compared', 0)}</div>
        </div>
        <div class="summary-card">
            <div class="label">Agreement Rate</div>
            <div class="value">{summary.get('agreement_rate', 0):.1f}%</div>
        </div>
        <div class="summary-card">
            <div class="label">Total Divergences</div>
            <div class="value">{summary.get('total_divergences', 0)}</div>
        </div>
        <div class="summary-card">
            <div class="label">Fill Comparisons</div>
            <div class="value">{summary.get('total_fill_comparisons', 0)}</div>
        </div>
        <div class="summary-card">
            <div class="label">Avg Entry Slippage</div>
            <div class="value">{summary.get('avg_entry_slippage_pts', 0):.3f}pt</div>
        </div>
        <div class="summary-card">
            <div class="label">Avg Fill Latency</div>
            <div class="value">{summary.get('avg_fill_latency_ms', 0):.0f}ms</div>
        </div>
    </div>

    <h2>Pass/Fail Checks</h2>
    <div class="checks">
        {checks_html}
        {reasons_html}
    </div>

    <h2>Divergence Breakdown</h2>
    <div class="checks">
        {cat_detail}
    </div>

    <h2>Trade Signals — Side-by-Side</h2>
    <table>
        <tr>
            <th>Timestamp</th><th>Bar</th>
            <th>Paper Dir</th><th>Live Dir</th>
            <th>Paper Score</th><th>Live Score</th>
            <th>Paper Entry</th><th>Live Entry</th>
            <th>Status</th>
        </tr>
        {trade_rows}
    </table>

    <h2>Fill Quality Comparison</h2>
    <table>
        <tr>
            <th>Trade ID</th><th>Dir</th>
            <th>Paper Price</th><th>Live Price</th>
            <th>Slippage</th><th>Latency</th>
            <th>Stop Match</th>
            <th>Paper PnL</th><th>Live PnL</th>
            <th>PnL Delta</th>
        </tr>
        {fill_rows}
    </table>

    <h2>Divergence Timeline</h2>
    <table>
        <tr>
            <th>Timestamp</th><th>Bar</th>
            <th>Severity</th><th>Category</th>
            <th>Description</th>
            <th>Paper</th><th>Live</th>
        </tr>
        {div_rows}
    </table>

    <div style="margin-top:30px; color:#444; font-size:0.8em; text-align:center">
        Generated by Paper-to-Live Comparison Framework
    </div>
</body>
</html>"""

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        f.write(html)
    logger.info("Report written to %s", p)


def main():
    parser = argparse.ArgumentParser(
        description="Generate paper-to-live comparison HTML report"
    )
    parser.add_argument(
        "--log", required=True,
        help="Path to comparison log JSON",
    )
    parser.add_argument(
        "--output", default="reports/comparison.html",
        help="Output HTML report path (default: reports/comparison.html)",
    )
    args = parser.parse_args()

    data = load_log(args.log)
    generate_report(data, args.output)


if __name__ == "__main__":
    main()
