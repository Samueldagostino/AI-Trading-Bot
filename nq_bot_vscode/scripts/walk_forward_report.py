"""
Walk-Forward Report — Data Structures & Reporting
====================================================
Defines FoldResult, WFSummary, and WalkForwardReport for
walk-forward optimization output, including JSON export,
HTML dashboard generation, and console summary.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class FoldResult:
    """Metrics for a single walk-forward fold."""
    fold_id: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_trades: int = 0
    test_trades: int = 0
    train_pf: float = 0.0
    test_pf: float = 0.0
    train_wr: float = 0.0
    test_wr: float = 0.0
    train_pnl: float = 0.0
    test_pnl: float = 0.0
    train_dd: float = 0.0
    test_dd: float = 0.0
    degradation: float = 0.0  # test_pf / train_pf
    skipped: bool = False
    skip_reason: str = ""
    # Optional: per-fold parameter info (for grid search)
    best_params: Optional[Dict] = None


@dataclass
class WFSummary:
    """Aggregate statistics across all walk-forward folds."""
    total_folds: int = 0
    valid_folds: int = 0
    skipped_folds: int = 0
    avg_train_pf: float = 0.0
    avg_test_pf: float = 0.0
    avg_train_wr: float = 0.0
    avg_test_wr: float = 0.0
    avg_degradation: float = 0.0
    consistency_pct: float = 0.0  # % of folds where OOS is profitable
    max_test_dd: float = 0.0
    total_test_pnl: float = 0.0
    total_test_trades: int = 0
    regime_breaks: int = 0  # folds where OOS PF < 1.0
    # Baseline comparison
    baseline_pass: bool = False
    baseline_reasons: List[str] = field(default_factory=list)


class WalkForwardReport:
    """Full walk-forward optimization report with export capabilities."""

    def __init__(self, folds: List[FoldResult], summary: WFSummary):
        self.folds = folds
        self.summary = summary
        self.generated_at = datetime.now(timezone.utc).isoformat()

    def to_json(self, path: str) -> None:
        """Export full report as JSON."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        report = {
            "generated_at": self.generated_at,
            "summary": asdict(self.summary),
            "folds": [asdict(f) for f in self.folds],
        }
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)

    def to_html(self, path: str) -> None:
        """Generate HTML dashboard with charts and summary table."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        valid_folds = [f for f in self.folds if not f.skipped]
        s = self.summary

        # Build fold data for charts
        fold_labels = [f"F{f.fold_id}" for f in valid_folds]
        train_pfs = [f.train_pf for f in valid_folds]
        test_pfs = [f.test_pf for f in valid_folds]
        train_wrs = [f.train_wr for f in valid_folds]
        test_wrs = [f.test_wr for f in valid_folds]
        degradations = [f.degradation for f in valid_folds]
        test_dds = [f.test_dd for f in valid_folds]

        # Rolling consistency: cumulative % of profitable OOS folds
        rolling_consistency = []
        profitable_count = 0
        for i, f in enumerate(valid_folds):
            if f.test_pf > 1.0:
                profitable_count += 1
            rolling_consistency.append(round(profitable_count / (i + 1) * 100, 1))

        # PASS/FAIL verdict
        if s.baseline_pass:
            verdict_class = "pass"
            verdict_text = "PASS"
        else:
            verdict_class = "fail"
            verdict_text = "FAIL"

        reasons_html = "".join(f"<li>{r}</li>" for r in s.baseline_reasons)

        # Folds table rows
        fold_rows = ""
        for f in self.folds:
            if f.skipped:
                fold_rows += f"""<tr class="skipped">
                    <td>{f.fold_id}</td><td>{f.train_start}</td><td>{f.train_end}</td>
                    <td>{f.test_start}</td><td>{f.test_end}</td>
                    <td colspan="7">SKIPPED: {f.skip_reason}</td></tr>"""
            else:
                pf_class = "good" if f.test_pf >= 1.0 else "bad"
                deg_class = "good" if f.degradation >= 0.6 else "bad"
                dd_class = "good" if f.test_dd <= 5.0 else "bad"
                fold_rows += f"""<tr>
                    <td>{f.fold_id}</td>
                    <td>{f.train_start}</td><td>{f.train_end}</td>
                    <td>{f.test_start}</td><td>{f.test_end}</td>
                    <td>{f.train_trades}</td><td>{f.test_trades}</td>
                    <td>{f.train_pf:.2f}</td>
                    <td class="{pf_class}">{f.test_pf:.2f}</td>
                    <td>{f.train_wr:.1f}%</td><td>{f.test_wr:.1f}%</td>
                    <td class="{deg_class}">{f.degradation:.2f}</td>
                    <td class="{dd_class}">{f.test_dd:.1f}%</td></tr>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Walk-Forward Optimization Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #1a1a2e; color: #e0e0e0; margin: 0; padding: 20px; }}
  h1, h2, h3 {{ color: #00d4ff; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }}
  .metric-card {{ background: #16213e; border-radius: 8px; padding: 15px; text-align: center; }}
  .metric-card .value {{ font-size: 2em; font-weight: bold; color: #00d4ff; }}
  .metric-card .label {{ font-size: 0.85em; color: #8892b0; margin-top: 5px; }}
  .verdict {{ padding: 20px; border-radius: 8px; margin: 20px 0; text-align: center; font-size: 1.5em; font-weight: bold; }}
  .verdict.pass {{ background: #0a3d0a; color: #4caf50; border: 2px solid #4caf50; }}
  .verdict.fail {{ background: #3d0a0a; color: #f44336; border: 2px solid #f44336; }}
  .verdict ul {{ text-align: left; font-size: 0.6em; font-weight: normal; }}
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 20px 0; }}
  .chart-box {{ background: #16213e; border-radius: 8px; padding: 15px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
  th {{ background: #16213e; padding: 10px; text-align: left; border-bottom: 2px solid #00d4ff; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #2a2a4a; }}
  tr:hover {{ background: #1f2b4d; }}
  tr.skipped {{ opacity: 0.5; }}
  .good {{ color: #4caf50; font-weight: bold; }}
  .bad {{ color: #f44336; font-weight: bold; }}
  canvas {{ max-height: 300px; }}
  .footer {{ text-align: center; color: #555; margin-top: 40px; font-size: 0.85em; }}
</style>
</head>
<body>
<div class="container">
<h1>Walk-Forward Optimization Report</h1>
<p style="color:#8892b0">Generated: {self.generated_at}</p>

<div class="verdict {verdict_class}">
  {verdict_text}
  <ul>{reasons_html}</ul>
</div>

<h2>Summary</h2>
<div class="summary-grid">
  <div class="metric-card"><div class="value">{s.total_folds}</div><div class="label">Total Folds</div></div>
  <div class="metric-card"><div class="value">{s.valid_folds}</div><div class="label">Valid Folds</div></div>
  <div class="metric-card"><div class="value">{s.avg_test_pf:.2f}</div><div class="label">Avg OOS PF</div></div>
  <div class="metric-card"><div class="value">{s.consistency_pct:.0f}%</div><div class="label">Consistency</div></div>
  <div class="metric-card"><div class="value">{s.avg_degradation:.2f}</div><div class="label">Avg Degradation</div></div>
  <div class="metric-card"><div class="value">{s.max_test_dd:.1f}%</div><div class="label">Max OOS DD</div></div>
  <div class="metric-card"><div class="value">${s.total_test_pnl:,.0f}</div><div class="label">Total OOS PnL</div></div>
  <div class="metric-card"><div class="value">{s.regime_breaks}</div><div class="label">Regime Breaks</div></div>
</div>

<h2>Charts</h2>
<div class="charts">
  <div class="chart-box"><h3>Profit Factor: In-Sample vs OOS</h3><canvas id="pfChart"></canvas></div>
  <div class="chart-box"><h3>Degradation Ratio (OOS PF / IS PF)</h3><canvas id="degChart"></canvas></div>
  <div class="chart-box"><h3>Rolling Consistency (%)</h3><canvas id="consChart"></canvas></div>
  <div class="chart-box"><h3>OOS Max Drawdown per Fold</h3><canvas id="ddChart"></canvas></div>
</div>

<h2>Fold Details</h2>
<table>
<thead><tr>
  <th>Fold</th><th>Train Start</th><th>Train End</th><th>Test Start</th><th>Test End</th>
  <th>Train #</th><th>Test #</th><th>Train PF</th><th>Test PF</th>
  <th>Train WR</th><th>Test WR</th><th>Deg Ratio</th><th>Test DD</th>
</tr></thead>
<tbody>{fold_rows}</tbody>
</table>

<div class="footer">Walk-Forward Optimization Framework — NQ Trading Bot</div>
</div>

<script>
const labels = {json.dumps(fold_labels)};
const trainPFs = {json.dumps(train_pfs)};
const testPFs = {json.dumps(test_pfs)};
const degradations = {json.dumps(degradations)};
const rollingCons = {json.dumps(rolling_consistency)};
const testDDs = {json.dumps(test_dds)};

const chartOpts = {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: '#e0e0e0' }} }} }},
  scales: {{ x: {{ ticks: {{ color: '#8892b0' }} }}, y: {{ ticks: {{ color: '#8892b0' }} }} }} }};

new Chart(document.getElementById('pfChart'), {{
  type: 'bar', data: {{
    labels, datasets: [
      {{ label: 'In-Sample PF', data: trainPFs, backgroundColor: '#1e88e5' }},
      {{ label: 'OOS PF', data: testPFs, backgroundColor: '#ff7043' }},
    ]
  }}, options: chartOpts
}});

new Chart(document.getElementById('degChart'), {{
  type: 'bar', data: {{
    labels, datasets: [{{ label: 'Degradation (OOS/IS)', data: degradations,
      backgroundColor: degradations.map(d => d >= 0.6 ? '#4caf50' : '#f44336') }}]
  }}, options: {{ ...chartOpts, plugins: {{ ...chartOpts.plugins,
    annotation: {{ annotations: {{ line1: {{ type: 'line', yMin: 0.6, yMax: 0.6, borderColor: '#ffeb3b', borderDash: [6,6] }} }} }} }} }}
}});

new Chart(document.getElementById('consChart'), {{
  type: 'line', data: {{
    labels, datasets: [{{ label: 'Rolling Consistency %', data: rollingCons,
      borderColor: '#00d4ff', fill: false, tension: 0.3 }}]
  }}, options: chartOpts
}});

new Chart(document.getElementById('ddChart'), {{
  type: 'bar', data: {{
    labels, datasets: [{{ label: 'OOS Max DD %', data: testDDs,
      backgroundColor: testDDs.map(d => d <= 5.0 ? '#4caf50' : '#f44336') }}]
  }}, options: chartOpts
}});
</script>
</body>
</html>"""

        with open(path, "w") as f:
            f.write(html)

    def print_summary(self) -> None:
        """Print key metrics to console."""
        s = self.summary
        valid = [f for f in self.folds if not f.skipped]

        print(f"\n{'=' * 70}")
        print(f"  WALK-FORWARD OPTIMIZATION RESULTS")
        print(f"{'=' * 70}")
        print(f"  Total Folds:        {s.total_folds} ({s.valid_folds} valid, {s.skipped_folds} skipped)")
        print(f"  Avg IS PF:          {s.avg_train_pf:.2f}")
        print(f"  Avg OOS PF:         {s.avg_test_pf:.2f}")
        print(f"  Avg IS WR:          {s.avg_train_wr:.1f}%")
        print(f"  Avg OOS WR:         {s.avg_test_wr:.1f}%")
        print(f"  Degradation Ratio:  {s.avg_degradation:.2f} (OOS PF / IS PF)")
        print(f"  Consistency:        {s.consistency_pct:.0f}% of folds profitable OOS")
        print(f"  Max OOS Drawdown:   {s.max_test_dd:.1f}%")
        print(f"  Total OOS PnL:      ${s.total_test_pnl:,.2f}")
        print(f"  Total OOS Trades:   {s.total_test_trades}")
        print(f"  Regime Breaks:      {s.regime_breaks} folds with OOS PF < 1.0")
        print(f"{'─' * 70}")

        # Per-fold table
        print(f"  {'Fold':>4}  {'Train':>12}  {'Test':>12}  {'IS PF':>6}  {'OOS PF':>7}  "
              f"{'IS WR':>6}  {'OOS WR':>7}  {'Deg':>5}  {'OOS DD':>6}")
        print(f"  {'─' * 66}")
        for f in self.folds:
            if f.skipped:
                print(f"  {f.fold_id:>4}  {f.train_start:>12}  {f.test_start:>12}  "
                      f"SKIPPED: {f.skip_reason}")
            else:
                pf_flag = " " if f.test_pf >= 1.0 else "*"
                print(f"  {f.fold_id:>4}  {f.train_start:>12}  {f.test_start:>12}  "
                      f"{f.train_pf:>6.2f}  {f.test_pf:>6.2f}{pf_flag} "
                      f"{f.train_wr:>5.1f}%  {f.test_wr:>5.1f}%  "
                      f"{f.degradation:>5.2f}  {f.test_dd:>5.1f}%")

        print(f"{'─' * 70}")

        # Verdict
        if s.baseline_pass:
            print(f"\n  VERDICT: PASS")
        else:
            print(f"\n  VERDICT: FAIL")
        for r in s.baseline_reasons:
            print(f"    - {r}")
        print(f"{'=' * 70}\n")
