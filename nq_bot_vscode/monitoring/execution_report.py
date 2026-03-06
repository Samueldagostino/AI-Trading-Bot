"""
Execution Report Generator
============================
Generates HTML reports for execution quality analysis.

Reports:
  - Daily: slippage/latency distributions, fill rate by hour, cost breakdown
  - Weekly: trend lines, day-over-day comparison, anomaly detection
  - Scaling readiness: projected slippage at 2x/4x/8x, market impact, verdict
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from monitoring.execution_analytics import ExecutionAnalytics, OrderEvent

logger = logging.getLogger(__name__)


class ExecutionReport:
    """Generates HTML reports from ExecutionAnalytics data."""

    def __init__(self, analytics: ExecutionAnalytics):
        self._analytics = analytics

    # ══════════════════════════════════════════════════════════
    # DAILY REPORT
    # ══════════════════════════════════════════════════════════

    def generate_daily(self, target_date: Optional[date] = None) -> str:
        """Generate daily execution quality report as HTML."""
        target = target_date or date.today()
        agg = self._analytics.get_daily_aggregate(target)
        worst = self._analytics.get_worst_fills(10)
        time_buckets = self._analytics.get_time_bucket_aggregates()
        type_agg = self._analytics.get_order_type_aggregates()
        dir_agg = self._analytics.get_direction_aggregates()

        # Get events for histograms
        events = [
            e for e in self._analytics.get_all_events()
            if e.order_sent_at and e.order_sent_at.date() == target
            and e.status in ("filled", "partial")
        ]
        slippages = [e.slippage_ticks for e in events if e.slippage_ticks is not None]
        latencies = [e.latency_ms for e in events if e.latency_ms is not None]

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Execution Report — {target}</title>
{self._css()}
</head>
<body>
<div class="container">
<h1>Execution Quality Report — {target}</h1>

{self._summary_card(agg)}

<div class="grid-2">
    <div class="card">
        <h2>Slippage Distribution (ticks)</h2>
        {self._histogram_table(slippages, "Slippage Ticks")}
    </div>
    <div class="card">
        <h2>Latency Distribution (ms)</h2>
        {self._histogram_table(latencies, "Latency ms")}
    </div>
</div>

<div class="card">
    <h2>Fill Rate by Hour (ET)</h2>
    {self._time_bucket_table(time_buckets)}
</div>

<div class="grid-2">
    <div class="card">
        <h2>Cost Breakdown by Order Type</h2>
        {self._agg_table(type_agg)}
    </div>
    <div class="card">
        <h2>Cost Breakdown by Direction</h2>
        {self._agg_table(dir_agg)}
    </div>
</div>

<div class="card">
    <h2>Worst Fills (Top 10 by Slippage)</h2>
    {self._worst_fills_table(worst)}
</div>

</div>
</body>
</html>"""
        return html

    # ══════════════════════════════════════════════════════════
    # WEEKLY REPORT
    # ══════════════════════════════════════════════════════════

    def generate_weekly(self, week_start: Optional[date] = None) -> str:
        """Generate weekly execution quality report as HTML."""
        start = week_start or (date.today() - timedelta(days=date.today().weekday()))
        end = start + timedelta(days=7)
        weekly_agg = self._analytics.get_weekly_aggregate(start)
        anomalies = self._analytics.detect_anomalies(sigma_threshold=3.0)

        # Day-over-day
        daily_data = []
        for i in range(7):
            d = start + timedelta(days=i)
            day_agg = self._analytics.get_daily_aggregate(d)
            daily_data.append((d, day_agg))

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Weekly Execution Report — {start} to {end - timedelta(days=1)}</title>
{self._css()}
</head>
<body>
<div class="container">
<h1>Weekly Execution Report — {start} to {end - timedelta(days=1)}</h1>

{self._summary_card(weekly_agg)}

<div class="card">
    <h2>Day-over-Day Comparison</h2>
    <table>
        <tr>
            <th>Date</th><th>Orders</th><th>Fill Rate</th>
            <th>Avg Slippage</th><th>Avg Latency</th><th>Avg Cost</th>
        </tr>
        {"".join(self._daily_row(d, a) for d, a in daily_data)}
    </table>
</div>

<div class="card">
    <h2>Anomalous Fills (> 3σ from mean)</h2>
    {self._anomalies_table(anomalies)}
</div>

</div>
</body>
</html>"""
        return html

    # ══════════════════════════════════════════════════════════
    # SCALING READINESS REPORT
    # ══════════════════════════════════════════════════════════

    def generate_scaling_readiness(self) -> str:
        """Generate scaling readiness assessment as HTML."""
        assessment = self._analytics.assess_scaling_readiness(lookback_days=30)
        ready = assessment["ready"]
        verdict_class = "verdict-go" if ready else "verdict-nogo"
        verdict_text = (
            f"Safe to scale to {assessment['safe_contracts']} contracts"
            if ready
            else f"Not ready — {assessment['reason']}"
        )

        projections = assessment.get("projections", {})
        proj_rows = ""
        for label, proj in projections.items():
            proj_rows += f"""<tr>
                <td>{label.replace('_', ' ')}</td>
                <td>{proj['projected_slippage_ticks']:.2f}</td>
                <td>${proj['projected_cost_per_trade']:.2f}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Scaling Readiness Assessment</title>
{self._css()}
</head>
<body>
<div class="container">
<h1>Scaling Readiness Assessment</h1>

<div class="card {verdict_class}">
    <h2>Verdict</h2>
    <p class="verdict-text">{verdict_text}</p>
</div>

<div class="card">
    <h2>Current Execution Metrics (Last {assessment.get('lookback_days', 30)} Days)</h2>
    <table>
        <tr><td>Sample Size</td><td><strong>{assessment.get('sample_size', 0)}</strong></td></tr>
        <tr><td>Avg Slippage</td><td>{assessment.get('avg_slippage_ticks', 0):.2f} ticks</td></tr>
        <tr><td>Std Slippage</td><td>{assessment.get('std_slippage_ticks', 0):.2f} ticks</td></tr>
        <tr><td>Max Slippage</td><td>{assessment.get('max_slippage_ticks', 0):.2f} ticks</td></tr>
        <tr><td>Avg Latency</td><td>{assessment.get('avg_latency_ms', 0):.1f} ms</td></tr>
        <tr><td>Fill Rate</td><td>{assessment.get('fill_rate_pct', 0):.1f}%</td></tr>
        <tr><td>Anomalous Fills</td><td>{assessment.get('anomaly_count', 0)}</td></tr>
    </table>
</div>

<div class="card">
    <h2>Projected Slippage at Scale</h2>
    <table>
        <tr><th>Contract Size</th><th>Projected Slippage (ticks)</th><th>Projected Cost/Trade</th></tr>
        {proj_rows}
    </table>
    <p class="note">Market impact model: slippage ∝ √(size multiplier)</p>
</div>

</div>
</body>
</html>"""
        return html

    # ══════════════════════════════════════════════════════════
    # HTML HELPERS
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _css() -> str:
        return """<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'SF Mono', 'Menlo', monospace; background: #0d1117; color: #c9d1d9; }
    .container { max-width: 1100px; margin: 0 auto; padding: 20px; }
    h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.4em; }
    h2 { color: #8b949e; margin-bottom: 12px; font-size: 1.1em; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
    th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #21262d; }
    th { color: #8b949e; font-weight: 600; }
    .metric { font-size: 1.6em; font-weight: bold; color: #58a6ff; }
    .metric-label { font-size: 0.75em; color: #8b949e; }
    .metrics-row { display: flex; gap: 20px; flex-wrap: wrap; }
    .metric-box { flex: 1; min-width: 120px; text-align: center; }
    .verdict-go { border-color: #238636; }
    .verdict-nogo { border-color: #da3633; }
    .verdict-text { font-size: 1.2em; font-weight: bold; padding: 10px 0; }
    .verdict-go .verdict-text { color: #3fb950; }
    .verdict-nogo .verdict-text { color: #f85149; }
    .bar { display: inline-block; background: #58a6ff; height: 14px; border-radius: 2px; }
    .note { color: #8b949e; font-size: 0.8em; margin-top: 8px; }
    .good { color: #3fb950; }
    .warn { color: #d29922; }
    .bad { color: #f85149; }
</style>"""

    def _summary_card(self, agg: Dict[str, Any]) -> str:
        fill_class = "good" if agg.get("fill_rate_pct", 0) >= 95 else "warn"
        slip_class = "good" if agg.get("avg_slippage_ticks", 0) <= 2 else "warn"
        return f"""<div class="card">
    <div class="metrics-row">
        <div class="metric-box">
            <div class="metric">{agg.get('total_orders', 0)}</div>
            <div class="metric-label">Total Orders</div>
        </div>
        <div class="metric-box">
            <div class="metric {fill_class}">{agg.get('fill_rate_pct', 0):.1f}%</div>
            <div class="metric-label">Fill Rate</div>
        </div>
        <div class="metric-box">
            <div class="metric {slip_class}">{agg.get('avg_slippage_ticks', 0):.2f}</div>
            <div class="metric-label">Avg Slippage (ticks)</div>
        </div>
        <div class="metric-box">
            <div class="metric">{agg.get('avg_latency_ms', 0):.0f}</div>
            <div class="metric-label">Avg Latency (ms)</div>
        </div>
        <div class="metric-box">
            <div class="metric">${agg.get('avg_cost_per_trade', 0):.2f}</div>
            <div class="metric-label">Avg Cost/Trade</div>
        </div>
        <div class="metric-box">
            <div class="metric">${agg.get('total_cost', 0):.2f}</div>
            <div class="metric-label">Total Cost</div>
        </div>
    </div>
</div>"""

    @staticmethod
    def _histogram_table(values: list, label: str) -> str:
        if not values:
            return "<p>No data</p>"
        # Simple bucket histogram
        min_v = min(values)
        max_v = max(values)
        if min_v == max_v:
            return f"<p>All values = {min_v}</p>"

        n_buckets = min(10, len(set(values)))
        step = (max_v - min_v) / n_buckets if n_buckets > 0 else 1
        buckets = {}
        for v in values:
            idx = min(int((v - min_v) / step), n_buckets - 1) if step > 0 else 0
            lo = round(min_v + idx * step, 1)
            hi = round(min_v + (idx + 1) * step, 1)
            key = f"{lo}–{hi}"
            buckets[key] = buckets.get(key, 0) + 1

        max_count = max(buckets.values()) if buckets else 1
        rows = ""
        for bucket, count in buckets.items():
            bar_width = int(count / max_count * 200) if max_count else 0
            rows += f'<tr><td>{bucket}</td><td>{count}</td><td><span class="bar" style="width:{bar_width}px"></span></td></tr>'

        return f"""<table>
    <tr><th>{label}</th><th>Count</th><th></th></tr>
    {rows}
</table>"""

    @staticmethod
    def _time_bucket_table(buckets: Dict[str, Dict[str, Any]]) -> str:
        rows = ""
        for label, agg in buckets.items():
            if agg["total_orders"] > 0:
                rows += f"""<tr>
                    <td>{label}</td>
                    <td>{agg['total_orders']}</td>
                    <td>{agg['fill_rate_pct']:.1f}%</td>
                    <td>{agg['avg_slippage_ticks']:.2f}</td>
                    <td>{agg['avg_latency_ms']:.0f} ms</td>
                    <td>${agg['avg_cost_per_trade']:.2f}</td>
                </tr>"""
        if not rows:
            return "<p>No data for time buckets</p>"
        return f"""<table>
    <tr><th>Time (ET)</th><th>Orders</th><th>Fill Rate</th><th>Avg Slip</th><th>Avg Latency</th><th>Avg Cost</th></tr>
    {rows}
</table>"""

    @staticmethod
    def _agg_table(agg_dict: Dict[str, Dict[str, Any]]) -> str:
        rows = ""
        for label, agg in agg_dict.items():
            if agg["total_orders"] > 0:
                rows += f"""<tr>
                    <td>{label}</td>
                    <td>{agg['total_orders']}</td>
                    <td>{agg['avg_slippage_ticks']:.2f}</td>
                    <td>${agg['avg_cost_per_trade']:.2f}</td>
                    <td>${agg['total_cost']:.2f}</td>
                </tr>"""
        if not rows:
            return "<p>No data</p>"
        return f"""<table>
    <tr><th>Type</th><th>Orders</th><th>Avg Slip (ticks)</th><th>Avg Cost</th><th>Total Cost</th></tr>
    {rows}
</table>"""

    @staticmethod
    def _worst_fills_table(fills: List[Dict[str, Any]]) -> str:
        if not fills:
            return "<p>No fill data</p>"
        rows = ""
        for f in fills:
            rows += f"""<tr>
                <td>{f['order_id']}</td>
                <td>{f['side']}</td>
                <td>{f.get('expected_price', 0):.2f}</td>
                <td>{f.get('fill_price', 0):.2f}</td>
                <td class="bad">{f.get('slippage_ticks', 0):.2f}</td>
                <td>{f.get('latency_ms', '-')}</td>
                <td>{f.get('order_type', '')}</td>
                <td>{f.get('timestamp', '')}</td>
            </tr>"""
        return f"""<table>
    <tr><th>Order ID</th><th>Side</th><th>Expected</th><th>Fill</th><th>Slippage</th><th>Latency</th><th>Type</th><th>Time</th></tr>
    {rows}
</table>"""

    @staticmethod
    def _anomalies_table(anomalies: List[OrderEvent]) -> str:
        if not anomalies:
            return "<p>No anomalous fills detected — execution quality is consistent.</p>"
        rows = ""
        for e in anomalies:
            rows += f"""<tr>
                <td>{e.order_id}</td>
                <td>{e.side}</td>
                <td>{e.expected_price:.2f}</td>
                <td>{e.fill_price:.2f if e.fill_price else '-'}</td>
                <td class="bad">{e.slippage_ticks:.2f if e.slippage_ticks else '-'}</td>
                <td>{e.order_sent_at.isoformat() if e.order_sent_at else '-'}</td>
            </tr>"""
        return f"""<table>
    <tr><th>Order ID</th><th>Side</th><th>Expected</th><th>Fill</th><th>Slippage (ticks)</th><th>Time</th></tr>
    {rows}
</table>"""

    @staticmethod
    def _daily_row(d: date, agg: Dict[str, Any]) -> str:
        if agg["total_orders"] == 0:
            return f'<tr><td>{d}</td><td colspan="5" style="color:#8b949e">No trades</td></tr>'
        return f"""<tr>
            <td>{d}</td>
            <td>{agg['total_orders']}</td>
            <td>{agg['fill_rate_pct']:.1f}%</td>
            <td>{agg['avg_slippage_ticks']:.2f}</td>
            <td>{agg['avg_latency_ms']:.0f} ms</td>
            <td>${agg['avg_cost_per_trade']:.2f}</td>
        </tr>"""
