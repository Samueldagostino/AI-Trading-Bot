"""
Multi-Period Backtest Runner
=============================
Runs the FULL production pipeline (ReplaySimulator with Variant C,
calibrated slippage, sweep detector, HC filter, HTF gate) on each
historical period and the existing baseline period.

Accepts --data-dir for a single period or runs all 6 periods with
--all flag.

Outputs standardized results JSON to logs/backtest_results/ and
generates a cross-period comparison report.

Usage:
    # Run single period
    python scripts/multi_period_backtest.py \\
        --data-dir data/firstrate/historical/aggregated/period_1_2021-09_to_2022-02

    # Run ALL periods (historical + baseline)
    python scripts/multi_period_backtest.py --all

    # Run all and generate comparison report
    python scripts/multi_period_backtest.py --all --report
"""

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
LOGS_DIR = REPO_ROOT / "logs" / "backtest_results"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Add project source to path
sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator

# ── Period definitions ──
PERIODS = OrderedDict([
    ("period_1", {
        "label": "Sep 2021 - Feb 2022",
        "regime": "bear_market",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_1_2021-09_to_2022-02"),
        "start": "2021-09-01",
        "end": "2022-03-01",
    }),
    ("period_2", {
        "label": "Mar 2022 - Aug 2022",
        "regime": "recovery",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_2_2022-03_to_2022-08"),
        "start": "2022-03-01",
        "end": "2022-09-01",
    }),
    ("period_3", {
        "label": "Sep 2022 - Aug 2023",
        "regime": "ai_rally",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_3_2022-09_to_2023-08"),
        "start": "2022-09-01",
        "end": "2023-09-01",
    }),
    ("period_4", {
        "label": "Sep 2023 - Feb 2024",
        "regime": "chop",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_4_2023-09_to_2024-02"),
        "start": "2023-09-01",
        "end": "2024-03-01",
    }),
    ("period_5", {
        "label": "Mar 2024 - Aug 2024",
        "regime": "election",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_5_2024-03_to_2024-08"),
        "start": "2024-03-01",
        "end": "2024-09-01",
    }),
    ("period_5b", {
        "label": "Sep 2024 - Aug 2025",
        "regime": "transition",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_5b_2024-09_to_2025-08"),
        "start": "2024-09-01",
        "end": "2025-09-01",
    }),
    ("period_6", {
        "label": "Sep 2025 - Feb 2026",
        "regime": "current",
        "data_dir": str(PROJECT_DIR / "data" / "firstrate"),
        "start": "2025-09-01",
        "end": "2026-03-01",
    }),
])


async def run_single_period(period_id: str, period: dict) -> dict:
    """Run the full production backtest on a single period.

    Uses ReplaySimulator with:
    - C1 variant C (trail from profit: 3pt threshold, 2.5pt trail)
    - Calibrated slippage (RTH 0.50pt, ETH 1.00pt)
    - Sweep detector enabled (additive)
    - HC filter >= 0.75, stop <= 30pts
    - HTF gate = 0.3
    """
    data_dir = period["data_dir"]

    if not Path(data_dir).exists():
        print(f"  ERROR: Data dir not found: {data_dir}")
        return {"error": "data_dir_not_found", "period": period_id}

    # Check for required 2m file
    nq_2m = Path(data_dir) / "NQ_2m.csv"
    if not nq_2m.exists():
        print(f"  ERROR: NQ_2m.csv not found in {data_dir}")
        return {"error": "no_2m_data", "period": period_id}

    sim = ReplaySimulator(
        speed="max",
        start_date=period.get("start"),
        end_date=period.get("end"),
        validate=True,
        data_dir=data_dir,
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )

    results = await sim.run()

    # ── Shadow-trade simulation (runs AFTER replay completes) ──
    shadow_analysis = sim._simulate_shadow_trades()

    # ── Build monthly breakdown from trades log ──
    monthly = {}
    for t in sim.state.trades_log:
        ts = t.get("timestamp", "")
        month = ts[:7]
        if not month:
            continue
        if month not in monthly:
            monthly[month] = {
                "trades": 0, "wins": 0, "pnl": 0.0,
                "c1_pnl": 0.0, "c2_pnl": 0.0,
                "gross_profit": 0.0, "gross_loss": 0.0,
                "sweep_trades": 0, "sweep_pnl": 0.0, "sweep_wins": 0,
                "confluence_trades": 0, "confluence_pnl": 0.0, "confluence_wins": 0,
                "slippage_pts": 0.0,
            }
        m = monthly[month]
        pnl = t.get("total_pnl", 0)
        m["trades"] += 1
        m["pnl"] += pnl
        m["c1_pnl"] += t.get("c1_pnl", 0)
        m["c2_pnl"] += t.get("c2_pnl", 0)
        m["slippage_pts"] += t.get("total_slippage_pts", 0)
        if pnl > 0:
            m["wins"] += 1
            m["gross_profit"] += pnl
        else:
            m["gross_loss"] += abs(pnl)

        src = t.get("signal_source", "signal")
        if src == "sweep":
            m["sweep_trades"] += 1
            m["sweep_pnl"] += pnl
            if pnl > 0:
                m["sweep_wins"] += 1
        elif src == "confluence":
            m["confluence_trades"] += 1
            m["confluence_pnl"] += pnl
            if pnl > 0:
                m["confluence_wins"] += 1

    # Build monthly table
    monthly_table = []
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = (m["wins"] / m["trades"] * 100) if m["trades"] > 0 else 0
        pf = (m["gross_profit"] / m["gross_loss"]) if m["gross_loss"] > 0 else (
            float('inf') if m["gross_profit"] > 0 else 0)
        monthly_table.append({
            "month": month,
            "trades": m["trades"],
            "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2) if pf < 100 else 999.99,
            "pnl": round(m["pnl"], 2),
            "c1_pnl": round(m["c1_pnl"], 2),
            "c2_pnl": round(m["c2_pnl"], 2),
            "sweep_trades": m["sweep_trades"],
            "sweep_pnl": round(m["sweep_pnl"], 2),
            "confluence_trades": m["confluence_trades"],
            "confluence_pnl": round(m["confluence_pnl"], 2),
        })

    # Sweep/confluence aggregate
    sweep_stats = results.get("sweep_stats", {})
    sweep_trades = sweep_stats.get("sweep_trades", 0)
    sweep_pnl = sweep_stats.get("sweep_pnl", 0.0)
    sweep_wr = sweep_stats.get("sweep_wr", 0.0)
    confluence_trades = sweep_stats.get("confluence_trades", 0)
    confluence_pnl = sweep_stats.get("confluence_pnl", 0.0)
    confluence_wr = sweep_stats.get("confluence_wr", 0.0)
    signal_only_trades = sweep_stats.get("signal_only_trades", 0)
    signal_only_pnl = sweep_stats.get("signal_only_pnl", 0.0)

    # Slippage
    slip = results.get("slippage", {})
    avg_slippage = slip.get("avg_slippage_per_fill", 0)

    # ── Build standardized output ──
    output = {
        "period_id": period_id,
        "label": period["label"],
        "regime": period["regime"],
        "date_range": f"{period.get('start', '?')} to {period.get('end', '?')}",
        "config": {
            "c1_variant": "C",
            "hc_min_score": 0.75,
            "hc_max_stop": 30.0,
            "htf_gate": 0.3,
            "sweep_detector": True,
            "slippage": "calibrated_v2",
        },
        "results": {
            "total_trades": results["total_trades"],
            "win_rate": results["win_rate"],
            "profit_factor": results["profit_factor"],
            "total_pnl": results["total_pnl"],
            "c1_pnl": results["c1_pnl"],
            "c2_pnl": results["c2_pnl"],
            "max_drawdown_pct": results["max_drawdown_pct"],
            "expectancy_per_trade": results["expectancy"],
            "avg_slippage_per_fill": avg_slippage,
        },
        "sweep_analysis": {
            "sweep_trades": sweep_trades,
            "sweep_pnl": round(sweep_pnl, 2),
            "sweep_wr": sweep_wr,
            "confluence_trades": confluence_trades,
            "confluence_pnl": round(confluence_pnl, 2),
            "confluence_wr": confluence_wr,
            "signal_only_trades": signal_only_trades,
            "signal_only_pnl": round(signal_only_pnl, 2),
        },
        "monthly_breakdown": monthly_table,
        "slippage_summary": slip,
        "shadow_analysis": shadow_analysis,
        "elapsed_seconds": results.get("elapsed_seconds", 0),
        "exec_bars": results.get("exec_bars", 0),
        "htf_bars": results.get("htf_bars", 0),
        "trades_log": sim.state.trades_log,
    }

    return output


def print_period_summary(output: dict) -> None:
    """Print a summary for a single period's backtest."""
    r = output.get("results", {})
    s = output.get("sweep_analysis", {})
    label = output.get("label", "?")
    regime = output.get("regime", "?")

    pf = r.get("profit_factor", 0)
    pf_str = f"{pf:.2f}" if pf < 100 else "inf"

    print(f"  {label} ({regime})")
    print(f"    Trades: {r['total_trades']:,} | WR: {r['win_rate']:.1f}% | PF: {pf_str}")
    print(f"    PnL: ${r['total_pnl']:+,.2f} | C1: ${r['c1_pnl']:+,.2f} | C2: ${r['c2_pnl']:+,.2f}")
    print(f"    Max DD: {r['max_drawdown_pct']:.1f}% | Expectancy: ${r['expectancy_per_trade']:.2f}")
    print(f"    Avg Slippage: {r.get('avg_slippage_per_fill', 0):.2f}pt/fill")
    print(f"    Sweep: {s.get('sweep_trades', 0)} trades (${s.get('sweep_pnl', 0):+,.0f}) | "
          f"Confluence: {s.get('confluence_trades', 0)} (${s.get('confluence_pnl', 0):+,.0f})")

    # Monthly breakdown
    monthly = output.get("monthly_breakdown", [])
    if monthly:
        print(f"    {'Month':<10} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL':>10}")
        for m in monthly:
            pf_m = m.get("profit_factor", 0)
            pf_m_str = f"{pf_m:.2f}" if pf_m < 100 else "inf"
            print(f"    {m['month']:<10} {m['trades']:>6} {m['win_rate']:>5.1f}% "
                  f"{pf_m_str:>6} ${m['pnl']:>+9,.0f}")

    # Shadow-trade gate ranking
    shadow = output.get("shadow_analysis", {})
    ranking = shadow.get("gate_value_ranking", [])
    if ranking:
        total_shadow = shadow.get("total_shadow_signals", 0)
        print(f"    Shadow signals: {total_shadow:,}")
        print(f"    {'Gate':<25} {'Count':>6} {'Shadow PnL':>12} {'Verdict':>12}")
        for g in ranking:
            print(f"    {g['gate']:<25} {g['count']:>6} "
                  f"${g['shadow_pnl']:>+10,.2f} {g['verdict']:>12}")


def generate_comparison_report(all_results: Dict[str, dict]) -> dict:
    """Generate the cross-period comparison report."""
    periods_data = []
    all_trades_log = []
    total_trades = 0
    total_pnl = 0.0
    total_c1 = 0.0
    total_c2 = 0.0
    total_gross_profit = 0.0
    total_gross_loss = 0.0
    total_wins = 0
    total_losses = 0
    max_dd_all = 0.0
    total_sweep = 0
    total_sweep_pnl = 0.0
    total_confluence = 0
    total_confluence_pnl = 0.0
    total_signal_only = 0
    total_signal_only_pnl = 0.0
    total_slippage_fills = 0
    total_slippage_pts = 0.0

    # Aggregated shadow-trade analysis
    agg_shadow_by_gate: Dict[str, Dict] = {}
    total_shadow_signals = 0

    flags = []
    best_period = None
    best_pf = -999
    worst_period = None
    worst_pf = 999

    for pid, output in all_results.items():
        if "error" in output:
            continue

        r = output.get("results", {})
        s = output.get("sweep_analysis", {})
        slip = output.get("slippage_summary", {})

        trades = r.get("total_trades", 0)
        pnl = r.get("total_pnl", 0)
        pf = r.get("profit_factor", 0)
        dd = r.get("max_drawdown_pct", 0)

        total_trades += trades
        total_pnl += pnl
        total_c1 += r.get("c1_pnl", 0)
        total_c2 += r.get("c2_pnl", 0)
        if dd > max_dd_all:
            max_dd_all = dd

        # Gross profit/loss from monthly
        for m in output.get("monthly_breakdown", []):
            total_gross_profit += m.get("pnl", 0) if m.get("pnl", 0) > 0 else 0
            total_gross_loss += abs(m.get("pnl", 0)) if m.get("pnl", 0) < 0 else 0

        # Actually compute from trades log for accuracy
        trades_log = output.get("trades_log", [])
        all_trades_log.extend(trades_log)
        period_gp = sum(t.get("total_pnl", 0) for t in trades_log if t.get("total_pnl", 0) > 0)
        period_gl = abs(sum(t.get("total_pnl", 0) for t in trades_log if t.get("total_pnl", 0) < 0))
        period_wins = sum(1 for t in trades_log if t.get("total_pnl", 0) > 0)
        period_losses = sum(1 for t in trades_log if t.get("total_pnl", 0) < 0)

        total_wins += period_wins
        total_losses += period_losses

        # Override gross_profit/loss with trade-level data
        total_gross_profit = sum(t.get("total_pnl", 0) for t in all_trades_log if t.get("total_pnl", 0) > 0)
        total_gross_loss = abs(sum(t.get("total_pnl", 0) for t in all_trades_log if t.get("total_pnl", 0) < 0))

        # Sweep stats
        total_sweep += s.get("sweep_trades", 0)
        total_sweep_pnl += s.get("sweep_pnl", 0)
        total_confluence += s.get("confluence_trades", 0)
        total_confluence_pnl += s.get("confluence_pnl", 0)
        total_signal_only += s.get("signal_only_trades", 0)
        total_signal_only_pnl += s.get("signal_only_pnl", 0)

        # Slippage
        total_slippage_fills += slip.get("total_fills", 0)
        total_slippage_pts += slip.get("total_slippage_points", 0)

        # Shadow analysis aggregation
        shadow = output.get("shadow_analysis", {})
        total_shadow_signals += shadow.get("total_shadow_signals", 0)
        for gate_name, gate_data in shadow.get("by_gate", {}).items():
            if gate_name not in agg_shadow_by_gate:
                agg_shadow_by_gate[gate_name] = {
                    "count": 0, "shadow_wins": 0, "shadow_losses": 0,
                    "shadow_timeouts": 0, "shadow_total_pnl": 0.0,
                }
            ag = agg_shadow_by_gate[gate_name]
            ag["count"] += gate_data.get("count", 0)
            ag["shadow_wins"] += gate_data.get("shadow_wins", 0)
            ag["shadow_losses"] += gate_data.get("shadow_losses", 0)
            ag["shadow_timeouts"] += gate_data.get("shadow_timeouts", 0)
            ag["shadow_total_pnl"] += gate_data.get("shadow_total_pnl", 0)

        # Flags
        if pf < 1.0:
            flags.append(f"WARNING: {pid} ({output.get('label')}) PF={pf:.2f} < 1.0 — system loses money")
        if dd > 5.0:
            flags.append(f"WARNING: {pid} ({output.get('label')}) MaxDD={dd:.1f}% > 5.0%")

        # Track best/worst
        if trades > 0:
            if pf > best_pf:
                best_pf = pf
                best_period = pid
            if pf < worst_pf:
                worst_pf = pf
                worst_period = pid

        periods_data.append({
            "period_id": pid,
            "label": output.get("label", ""),
            "regime": output.get("regime", ""),
            "total_trades": trades,
            "win_rate": r.get("win_rate", 0),
            "profit_factor": pf,
            "total_pnl": round(pnl, 2),
            "c1_pnl": round(r.get("c1_pnl", 0), 2),
            "c2_pnl": round(r.get("c2_pnl", 0), 2),
            "max_drawdown_pct": round(dd, 1),
            "expectancy": round(r.get("expectancy_per_trade", 0), 2),
            "avg_slippage": round(r.get("avg_slippage_per_fill", 0), 2),
            "sweep_trades": s.get("sweep_trades", 0),
            "sweep_pnl": round(s.get("sweep_pnl", 0), 2),
            "confluence_trades": s.get("confluence_trades", 0),
            "confluence_pnl": round(s.get("confluence_pnl", 0), 2),
        })

    # Aggregate
    agg_pf = round(total_gross_profit / total_gross_loss, 2) if total_gross_loss > 0 else 0
    agg_wr = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0
    agg_exp = round(total_pnl / total_trades, 2) if total_trades > 0 else 0
    avg_slippage_overall = round(total_slippage_pts / total_slippage_fills, 2) if total_slippage_fills > 0 else 0

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "periods": periods_data,
        "aggregate": {
            "total_periods": len(periods_data),
            "total_trades": total_trades,
            "win_rate": agg_wr,
            "profit_factor": agg_pf,
            "total_pnl": round(total_pnl, 2),
            "c1_pnl": round(total_c1, 2),
            "c2_pnl": round(total_c2, 2),
            "max_drawdown_pct": round(max_dd_all, 1),
            "expectancy_per_trade": agg_exp,
            "avg_slippage_per_fill": avg_slippage_overall,
            "total_sweep_trades": total_sweep,
            "total_sweep_pnl": round(total_sweep_pnl, 2),
            "total_confluence_trades": total_confluence,
            "total_confluence_pnl": round(total_confluence_pnl, 2),
            "total_signal_only_trades": total_signal_only,
            "total_signal_only_pnl": round(total_signal_only_pnl, 2),
        },
        "best_period": {
            "id": best_period,
            "label": all_results.get(best_period, {}).get("label", "?") if best_period else "?",
            "profit_factor": round(best_pf, 2) if best_period else 0,
        },
        "worst_period": {
            "id": worst_period,
            "label": all_results.get(worst_period, {}).get("label", "?") if worst_period else "?",
            "profit_factor": round(worst_pf, 2) if worst_period else 0,
        },
        "flags": flags,
        "shadow_analysis": {
            "total_shadow_signals": total_shadow_signals,
            "by_gate": {
                gate_name: {
                    "count": g["count"],
                    "shadow_wins": g["shadow_wins"],
                    "shadow_losses": g["shadow_losses"],
                    "shadow_timeouts": g["shadow_timeouts"],
                    "shadow_total_pnl": round(g["shadow_total_pnl"], 2),
                    "verdict": "PROTECTING" if g["shadow_total_pnl"] < 0 else "COSTING",
                }
                for gate_name, g in agg_shadow_by_gate.items()
            },
            "gate_value_ranking": sorted(
                [
                    {
                        "gate": gn,
                        "shadow_pnl": round(gd["shadow_total_pnl"], 2),
                        "count": gd["count"],
                        "verdict": "PROTECTING" if gd["shadow_total_pnl"] < 0 else "COSTING",
                    }
                    for gn, gd in agg_shadow_by_gate.items()
                ],
                key=lambda x: x["shadow_pnl"],
            ),
        },
    }

    return report, all_trades_log


def run_monte_carlo(trades_log: List[dict], n_simulations: int = 1000,
                    account_size: float = 50000.0) -> dict:
    """Monte Carlo simulation on trade outcomes.

    Shuffles trade PnL outcomes and measures distribution of:
    - Profit factor
    - Max drawdown
    - Probability of ruin (account hitting -20%)
    - Monthly PnL confidence intervals
    """
    pnl_values = [t.get("total_pnl", 0) for t in trades_log]
    n_trades = len(pnl_values)

    if n_trades < 50:
        return {"error": "insufficient_trades", "n_trades": n_trades}

    rng = random.Random(42)
    pf_values = []
    max_dd_values = []
    ruin_count = 0
    final_equities = []

    for sim_i in range(n_simulations):
        # Shuffle trade order
        shuffled = pnl_values.copy()
        rng.shuffle(shuffled)

        equity = account_size
        peak = account_size
        max_dd = 0.0
        ruined = False
        gross_profit = 0.0
        gross_loss = 0.0

        for pnl in shuffled:
            equity += pnl
            if pnl > 0:
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)

            if equity > peak:
                peak = equity

            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd

            # Ruin = account drops 20% from starting equity
            if equity <= account_size * 0.80:
                ruined = True

        pf = gross_profit / gross_loss if gross_loss > 0 else 999.99
        pf_values.append(pf)
        max_dd_values.append(max_dd)
        final_equities.append(equity)
        if ruined:
            ruin_count += 1

    # Sort for percentiles
    pf_values.sort()
    max_dd_values.sort()
    final_equities.sort()

    # Monthly PnL estimate
    # Group original trades by month
    monthly_pnl = {}
    for t in trades_log:
        ts = t.get("timestamp", "")
        month = ts[:7]
        if month:
            monthly_pnl.setdefault(month, 0.0)
            monthly_pnl[month] += t.get("total_pnl", 0)

    monthly_values = sorted(monthly_pnl.values())
    n_months = len(monthly_values)
    monthly_mean = sum(monthly_values) / n_months if n_months > 0 else 0
    monthly_std = (sum((v - monthly_mean) ** 2 for v in monthly_values) / max(1, n_months - 1)) ** 0.5 if n_months > 1 else 0

    def percentile(sorted_list, pct):
        idx = int(len(sorted_list) * pct / 100)
        idx = max(0, min(idx, len(sorted_list) - 1))
        return sorted_list[idx]

    return {
        "n_simulations": n_simulations,
        "n_trades": n_trades,
        "pf_5th_percentile": round(percentile(pf_values, 5), 2),
        "pf_median": round(percentile(pf_values, 50), 2),
        "pf_95th_percentile": round(percentile(pf_values, 95), 2),
        "max_dd_5th_percentile": round(percentile(max_dd_values, 5), 1),
        "max_dd_median": round(percentile(max_dd_values, 50), 1),
        "max_dd_95th_percentile": round(percentile(max_dd_values, 95), 1),
        "probability_of_ruin": round(ruin_count / n_simulations * 100, 2),
        "monthly_pnl_mean": round(monthly_mean, 2),
        "monthly_pnl_std": round(monthly_std, 2),
        "monthly_pnl_95ci_low": round(monthly_mean - 1.96 * monthly_std, 2),
        "monthly_pnl_95ci_high": round(monthly_mean + 1.96 * monthly_std, 2),
        "final_equity_5th": round(percentile(final_equities, 5), 2),
        "final_equity_median": round(percentile(final_equities, 50), 2),
        "final_equity_95th": round(percentile(final_equities, 95), 2),
    }


async def run_all_periods() -> Dict[str, dict]:
    """Run backtest on all 6 periods sequentially."""
    all_results = OrderedDict()

    for pid, period in PERIODS.items():
        print(f"\n{'=' * 70}")
        print(f"  PERIOD: {pid} — {period['label']} ({period['regime']})")
        print(f"  Data: {period['data_dir']}")
        print(f"{'=' * 70}")

        t0 = time.time()
        output = await run_single_period(pid, period)
        elapsed = time.time() - t0

        if "error" in output:
            print(f"  FAILED: {output['error']}")
            all_results[pid] = output
            continue

        # Save individual results
        result_file = LOGS_DIR / f"{pid}_results.json"
        # Don't include trades_log in saved file (too large)
        save_output = {k: v for k, v in output.items() if k != "trades_log"}
        with open(str(result_file), "w") as f:
            json.dump(save_output, f, indent=2, default=str)

        print(f"\n  Completed in {elapsed:.1f}s")
        print_period_summary(output)
        print(f"  Saved: {result_file}")

        all_results[pid] = output

    return all_results


async def main():
    parser = argparse.ArgumentParser(
        description="Multi-Period Backtest — Full Production Pipeline"
    )
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="Data directory for single period backtest"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run all 6 periods"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Generate cross-period comparison report"
    )
    parser.add_argument(
        "--monte-carlo", action="store_true",
        help="Run Monte Carlo simulation if >3000 total trades"
    )
    args = parser.parse_args()

    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\n{'#' * 70}")
    print(f"  MULTI-PERIOD BACKTEST — FULL PRODUCTION PIPELINE")
    print(f"  Config: Variant C + Calibrated Slippage + Sweep Detector")
    print(f"  HC: score>=0.75, stop<=30pts | HTF gate: 0.3")
    print(f"{'#' * 70}")

    if args.all or args.report or args.monte_carlo:
        all_results = await run_all_periods()

        # ── Comparison Report ──
        print(f"\n\n{'#' * 70}")
        print(f"  CROSS-PERIOD COMPARISON")
        print(f"{'#' * 70}\n")

        report, all_trades = generate_comparison_report(all_results)

        # Print comparison table
        print(f"  {'Period':<12} {'Label':<25} {'Trades':>6} {'WR':>6} {'PF':>6} "
              f"{'PnL':>10} {'C1':>9} {'C2':>9} {'MaxDD':>6} {'Exp':>7}")
        print(f"  {'─' * 98}")

        for p in report["periods"]:
            pf = p["profit_factor"]
            pf_str = f"{pf:.2f}" if pf < 100 else "inf"
            print(f"  {p['period_id']:<12} {p['label']:<25} {p['total_trades']:>6} "
                  f"{p['win_rate']:>5.1f}% {pf_str:>6} ${p['total_pnl']:>+9,.0f} "
                  f"${p['c1_pnl']:>+8,.0f} ${p['c2_pnl']:>+8,.0f} "
                  f"{p['max_drawdown_pct']:>5.1f}% ${p['expectancy']:>6.2f}")

        agg = report["aggregate"]
        print(f"  {'─' * 98}")
        pf_agg = agg["profit_factor"]
        pf_agg_str = f"{pf_agg:.2f}" if pf_agg < 100 else "inf"
        print(f"  {'AGGREGATE':<12} {'ALL PERIODS':<25} {agg['total_trades']:>6} "
              f"{agg['win_rate']:>5.1f}% {pf_agg_str:>6} ${agg['total_pnl']:>+9,.0f} "
              f"${agg['c1_pnl']:>+8,.0f} ${agg['c2_pnl']:>+8,.0f} "
              f"{agg['max_drawdown_pct']:>5.1f}% ${agg['expectancy_per_trade']:>6.2f}")

        # Best/worst
        print(f"\n  Strongest regime: {report['best_period']['label']} "
              f"(PF {report['best_period']['profit_factor']:.2f})")
        print(f"  Weakest regime:   {report['worst_period']['label']} "
              f"(PF {report['worst_period']['profit_factor']:.2f})")

        # Flags
        if report["flags"]:
            print(f"\n  FLAGS:")
            for flag in report["flags"]:
                print(f"    {flag}")

        # ── Aggregated Shadow-Trade Analysis ──
        shadow_agg = report.get("shadow_analysis", {})
        shadow_ranking = shadow_agg.get("gate_value_ranking", [])
        if shadow_ranking:
            print(f"\n{'=' * 70}")
            print(f"  SHADOW-TRADE ANALYSIS (ALL PERIODS) — "
                  f"{shadow_agg.get('total_shadow_signals', 0):,} rejected signals")
            print(f"{'=' * 70}")
            print(f"  {'Gate':<25} {'Count':>6} {'Shadow PnL':>12} {'Verdict':>12}")
            print(f"  {'─' * 58}")
            for g in shadow_ranking:
                print(f"  {g['gate']:<25} {g['count']:>6} "
                      f"${g['shadow_pnl']:>+10,.2f} {g['verdict']:>12}")

        # ── Monte Carlo ──
        mc_results = None
        if agg["total_trades"] > 3000 or args.monte_carlo:
            print(f"\n{'=' * 70}")
            print(f"  MONTE CARLO SIMULATION ({len(all_trades):,} trades)")
            print(f"{'=' * 70}")

            mc_results = run_monte_carlo(all_trades)

            if "error" not in mc_results:
                print(f"  Simulations:           {mc_results['n_simulations']:,}")
                print(f"  Trade count:           {mc_results['n_trades']:,}")
                print(f"  PF 5th percentile:     {mc_results['pf_5th_percentile']:.2f}")
                print(f"  PF median:             {mc_results['pf_median']:.2f}")
                print(f"  PF 95th percentile:    {mc_results['pf_95th_percentile']:.2f}")
                print(f"  Max DD 5th pctl:       {mc_results['max_dd_5th_percentile']:.1f}%")
                print(f"  Max DD median:         {mc_results['max_dd_median']:.1f}%")
                print(f"  Max DD 95th pctl:      {mc_results['max_dd_95th_percentile']:.1f}%")
                print(f"  Probability of ruin:   {mc_results['probability_of_ruin']:.2f}%")
                print(f"  Monthly PnL mean:      ${mc_results['monthly_pnl_mean']:+,.2f}")
                print(f"  Monthly PnL 95% CI:    ${mc_results['monthly_pnl_95ci_low']:+,.2f} to "
                      f"${mc_results['monthly_pnl_95ci_high']:+,.2f}")
            else:
                print(f"  Insufficient trades: {mc_results.get('n_trades', 0)}")

            report["monte_carlo"] = mc_results

        # Save report
        report_path = REPO_ROOT / "logs" / "multi_period_report.json"
        # Remove trades_log from report to keep file size manageable
        save_report = {k: v for k, v in report.items()}
        with open(str(report_path), "w") as f:
            json.dump(save_report, f, indent=2, default=str)
        print(f"\n  Report saved: {report_path}")

    elif args.data_dir:
        # Single period
        period = {
            "label": "Custom",
            "regime": "unknown",
            "data_dir": args.data_dir,
            "start": None,
            "end": None,
        }
        output = await run_single_period("custom", period)
        if "error" not in output:
            print_period_summary(output)
            result_file = LOGS_DIR / "custom_results.json"
            save_output = {k: v for k, v in output.items() if k != "trades_log"}
            with open(str(result_file), "w") as f:
                json.dump(save_output, f, indent=2, default=str)
            print(f"\n  Saved: {result_file}")
        else:
            print(f"\n  ERROR: {output['error']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
