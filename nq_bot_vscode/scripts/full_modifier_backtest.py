#!/usr/bin/env python3
"""
Full Modifier Backtest — All 4 Institutional Modifiers Active
==============================================================
Runs one comprehensive backtest with ALL institutional modifiers
enabled and compares against the provided baseline metrics.

Modifiers:
  1. Overnight Bias   — prev close vs current open alignment with HTF
  2. FOMC Drift       — pre-FOMC position sizing / stand-aside
  3. Gamma Regime     — VIX term structure (neutral without VIX data)
  4. Volatility HAR-RV — realized vol regime from 5-min returns

Output:
  - logs/full_modifier_backtest_results.json
  - logs/per_trade_modifier_log.json
  - Comparison table and modifier activity summary to stdout
"""

import asyncio
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator
from signals.volatility_forecast import HARRVForecaster

ET = ZoneInfo("America/New_York")
LOGS_DIR = PROJECT_DIR / "logs"

# Baseline provided in task spec
BASELINE = {
    "total_trades": 2143,
    "win_rate": 53.50,
    "profit_factor": 1.60,
    "total_pnl": 36179.0,
    "c1_pnl": 14811.0,
    "c2_pnl": 21367.0,
    "max_drawdown_pct": 1.64,
    "sharpe_approx": 2.24,
    "expectancy": 16.88,
}


# ── Helpers ──────────────────────────────────────────────────────────

def precompute_daily_rvs(data_dir: str) -> dict:
    """Compute daily realized volatility from 5-min NQ bar data."""
    from data_pipeline.pipeline import TradingViewImporter
    from config.settings import CONFIG

    csv_path = Path(data_dir) / "NQ_5m.csv"
    if not csv_path.exists():
        print(f"  WARNING: No 5-min data at {csv_path} — vol modifier stays neutral")
        return {}

    importer = TradingViewImporter(CONFIG)
    bars = importer.import_file(str(csv_path))
    if not bars:
        return {}

    # Group bars by ET trading day
    daily_bars = defaultdict(list)
    for bar in bars:
        day_str = bar.timestamp.astimezone(ET).date().isoformat()
        daily_bars[day_str].append(bar)

    # Compute RV per day from 5-min log returns
    daily_rvs = {}
    for day_str in sorted(daily_bars.keys()):
        day_sorted = sorted(daily_bars[day_str], key=lambda b: b.timestamp)
        if len(day_sorted) < 2:
            continue
        returns = []
        for i in range(1, len(day_sorted)):
            if day_sorted[i - 1].close > 0:
                returns.append(math.log(day_sorted[i].close / day_sorted[i - 1].close))
        if returns:
            daily_rvs[day_str] = HARRVForecaster.compute_realized_volatility(returns)

    return daily_rvs


def compute_sharpe(trade_pnls: list) -> float:
    """Sharpe approximation: mean/std * sqrt(252)."""
    if len(trade_pnls) < 2:
        return 0.0
    import numpy as np
    arr = np.array(trade_pnls, dtype=float)
    if arr.std() == 0:
        return 0.0
    return round(float(arr.mean() / arr.std() * (252 ** 0.5)), 2)


# ── Main ─────────────────────────────────────────────────────────────

async def main():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Clear stale modifier log files (they use append mode)
    for fn in ("institutional_modifiers_log.json", "modifier_decisions.json"):
        p = LOGS_DIR / fn
        if p.exists():
            p.unlink()

    print("=" * 75)
    print("  FULL MODIFIER BACKTEST — All 4 Institutional Modifiers ACTIVE")
    print("=" * 75)
    print()
    print("  Modifiers:")
    print("    1. Overnight Bias   — tracks prev close vs current open")
    print("    2. FOMC Drift       — uses 2025-2026 FOMC calendar")
    print("    3. Gamma Regime     — returns 1.0x (no VIX data in CSVs)")
    print("    4. Volatility HAR-RV — fed from 5-min RV; activates after 22 days")
    print()

    # ── Pre-compute daily RVs for vol forecaster ──
    data_dir = str(PROJECT_DIR / "data" / "firstrate")
    print("  Pre-computing daily realized volatility from 5-min data...")
    daily_rvs = precompute_daily_rvs(data_dir)
    print(f"  Computed {len(daily_rvs)} daily RV values")
    print()

    # ── Create simulator ──
    sim = ReplaySimulator(
        speed="max",
        start_date="2025-09-01",
        end_date="2026-03-01",
        validate=False,
        quiet=True,
        modifiers_enabled=True,
    )

    # ── Per-trade modifier capture state ──
    per_trade_log = []
    _pending = {}

    # ── Patch 1: inject vol-forecaster feeding after bot init ──
    _orig_patch_slip = sim._patch_executor_slippage

    def _enhanced_patch_slip():
        _orig_patch_slip()

        if daily_rvs:
            fc = sim.bot._institutional_engine.vol_forecaster
            oos_start = "2025-09-01"
            pre_dates = sorted(d for d in daily_rvs if d < oos_start)

            # Pre-warm with last 30 days before OOS window
            for d in pre_dates[-30:]:
                fc.update(daily_rvs[d])
                fc.forecast()

            print(f"  Vol forecaster pre-warmed: {len(pre_dates[-30:])} days, "
                  f"has_enough_data={fc.has_enough_data}")

            # Patch daily reset to feed ongoing RVs
            _orig_reset = sim.state.reset_daily

            def _feeding_reset(date_str):
                _orig_reset(date_str)
                try:
                    from datetime import date as dcls
                    d = dcls.fromisoformat(date_str)
                    for off in range(1, 6):
                        prev = (d - timedelta(days=off)).isoformat()
                        if prev in daily_rvs:
                            fc.update(daily_rvs[prev])
                            fc.forecast()
                            break
                except Exception:
                    pass

            sim.state.reset_daily = _feeding_reset

    sim._patch_executor_slippage = _enhanced_patch_slip

    # ── Patch 2: capture modifier data at entry ──
    _orig_handle = sim._handle_result

    def _patched_handle(result, timestamp):
        if result.get("action") == "entry":
            _pending.clear()
            _pending["entry_ts"] = timestamp.isoformat()
            _pending["direction"] = result.get("direction", "")
            _pending["entry_price"] = result.get("entry_price", 0)
            _pending["total_mult"] = result.get("inst_position_mult", 1.0)
            _pending["stop_mult"] = result.get("inst_stop_mult", 1.0)
            _pending["runner_mult"] = result.get("inst_runner_mult", 1.0)
            _pending["overnight_class"] = result.get("inst_overnight", "n/a")
            _pending["fomc_window"] = result.get("inst_fomc_window", "n/a")
        _orig_handle(result, timestamp)

    sim._handle_result = _patched_handle

    # ── Patch 3: store per-trade modifier log on trade close ──
    _orig_record = sim._record_trade_result

    def _patched_record(result, timestamp):
        entry_data = dict(_pending)
        _orig_record(result, timestamp)

        per_trade_log.append({
            "trade_id": len(per_trade_log) + 1,
            "close_timestamp": timestamp.isoformat(),
            "entry_timestamp": entry_data.get("entry_ts", ""),
            "direction": entry_data.get("direction", result.get("direction", "")),
            "entry_price": entry_data.get("entry_price", result.get("entry_price", 0)),
            "pnl": result.get("total_pnl", 0),
            "total_mult": entry_data.get("total_mult", 1.0),
            "stop_mult": entry_data.get("stop_mult", 1.0),
            "runner_mult": entry_data.get("runner_mult", 1.0),
            "overnight_class": entry_data.get("overnight_class", "n/a"),
            "fomc_window": entry_data.get("fomc_window", "n/a"),
            # Individual mults filled in post-run from modifier log
            "overnight_mult": 1.0,
            "fomc_mult": 1.0,
            "gamma_mult": 1.0,
            "vol_mult": 1.0,
        })
        _pending.clear()

    sim._record_trade_result = _patched_record

    # ── Run simulation ──
    print("  Running full backtest (Sep 2025 – Feb 2026)...")
    print()
    results = await sim.run()
    print()
    print("  Backtest complete.")
    print()

    # ── Post-process: enrich per-trade log with individual multipliers ──
    modifier_log_path = LOGS_DIR / "institutional_modifiers_log.json"
    ts_to_mods = {}
    if modifier_log_path.exists():
        with open(modifier_log_path) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    ts_to_mods[e["timestamp"]] = e
                except (json.JSONDecodeError, KeyError):
                    continue

    for trade in per_trade_log:
        entry_ts = trade.get("entry_timestamp", "")
        if entry_ts in ts_to_mods:
            m = ts_to_mods[entry_ts]
            trade["overnight_mult"] = m.get("overnight", {}).get("position", 1.0)
            trade["fomc_mult"] = m.get("fomc", {}).get("position", 1.0)
            trade["gamma_mult"] = m.get("gamma", {}).get("position", 1.0)
            trade["vol_mult"] = m.get("volatility", {}).get("position", 1.0)

    # ── Extract metrics ──
    st = sim.state
    trade_pnls = [t.get("total_pnl", 0) for t in st.trades_log if "total_pnl" in t]

    modified = {
        "total_trades": st.total_trades,
        "win_rate": round(st.win_rate, 2),
        "profit_factor": round(st.profit_factor, 2),
        "total_pnl": round(st.total_pnl, 2),
        "c1_pnl": round(st.c1_pnl, 2),
        "c2_pnl": round(st.c2_pnl, 2),
        "max_drawdown_pct": round(st.max_drawdown_pct, 2),
        "sharpe_approx": compute_sharpe(trade_pnls),
        "expectancy": round(st.expectancy, 2),
    }

    # ── Save JSON outputs ──
    with open(LOGS_DIR / "full_modifier_backtest_results.json", "w") as f:
        json.dump({
            "label": "all_modifiers_active",
            "period": "2025-09-01 to 2026-03-01",
            "modifiers_active": [
                "overnight_bias", "fomc_drift", "gamma_regime", "volatility_har_rv",
            ],
            **modified,
            "baseline": BASELINE,
            "timestamp": datetime.now(ET).isoformat(),
        }, f, indent=2, default=str)

    # Re-save per-trade log with enriched individual multipliers
    with open(LOGS_DIR / "per_trade_modifier_log.json", "w") as f:
        json.dump(per_trade_log, f, indent=2, default=str)

    # ────────────────────────────────────────────────────────────────
    #  COMPARISON TABLE
    # ────────────────────────────────────────────────────────────────
    print("=" * 75)
    print("  COMPARISON: BASELINE vs ALL MODIFIERS ACTIVE")
    print("=" * 75)
    print(f"  {'Metric':<25} {'Baseline':>12} {'All Modifiers':>14} {'Delta':>10}")
    print("  " + "-" * 63)

    rows = [
        ("Total Trades",      "total_trades"),
        ("Win Rate (%)",      "win_rate"),
        ("Profit Factor",     "profit_factor"),
        ("Total PnL ($)",     "total_pnl"),
        ("C1 PnL ($)",        "c1_pnl"),
        ("C2 PnL ($)",        "c2_pnl"),
        ("Max Drawdown (%)",  "max_drawdown_pct"),
        ("Sharpe (approx)",   "sharpe_approx"),
        ("Expectancy ($)",    "expectancy"),
    ]
    for label, key in rows:
        b = BASELINE[key]
        m = modified[key]
        d = m - b
        if isinstance(b, int) and isinstance(m, int):
            print(f"  {label:<25} {b:>12,} {m:>14,} {d:>+10,}")
        else:
            print(f"  {label:<25} {b:>12,.2f} {m:>14,.2f} {d:>+10,.2f}")
    print("  " + "=" * 63)

    # ────────────────────────────────────────────────────────────────
    #  MODIFIER ACTIVITY SUMMARY
    # ────────────────────────────────────────────────────────────────
    total = len(per_trade_log)
    on_active = sum(1 for t in per_trade_log if t["overnight_mult"] != 1.0)
    fomc_active = sum(1 for t in per_trade_log if t["fomc_mult"] != 1.0)
    gamma_active = sum(1 for t in per_trade_log if t["gamma_mult"] != 1.0)
    vol_active = sum(1 for t in per_trade_log if t["vol_mult"] != 1.0)
    stand_asides = sum(1 for e in ts_to_mods.values() if e.get("stand_aside"))

    def pct(n):
        return f"{n / total * 100:.1f}%" if total > 0 else "n/a"

    print()
    print("=" * 75)
    print("  MODIFIER ACTIVITY SUMMARY")
    print("=" * 75)
    print(f"  Overnight modifier != 1.0:   {on_active:>5} / {total} ({pct(on_active)})")
    print(f"  FOMC modifier != 1.0:        {fomc_active:>5} / {total} ({pct(fomc_active)})")
    print(f"  Gamma modifier != 1.0:       {gamma_active:>5} / {total} ({pct(gamma_active)})  "
          f"[expected 0 — no VIX data]")
    print(f"  Vol modifier != 1.0:         {vol_active:>5} / {total} ({pct(vol_active)})")
    print(f"  FOMC stand-aside blocks:     {stand_asides:>5}")
    print()

    # ────────────────────────────────────────────────────────────────
    #  ASSESSMENT
    # ────────────────────────────────────────────────────────────────
    pf_d = modified["profit_factor"] - BASELINE["profit_factor"]
    pnl_d = modified["total_pnl"] - BASELINE["total_pnl"]
    dd_d = modified["max_drawdown_pct"] - BASELINE["max_drawdown_pct"]
    sh_d = modified["sharpe_approx"] - BASELINE["sharpe_approx"]

    if pf_d > 0.05 and pnl_d > 500:
        verdict = "IMPROVED"
    elif pf_d < -0.1 or pnl_d < -2000:
        verdict = "DEGRADED"
    else:
        verdict = "MAINTAINED"

    print("=" * 75)
    print("  ASSESSMENT")
    print("=" * 75)
    print(f"  Verdict: {verdict}")
    print(f"    Profit Factor:  {BASELINE['profit_factor']:.2f} -> "
          f"{modified['profit_factor']:.2f} ({pf_d:+.2f})")
    print(f"    Total PnL:      ${BASELINE['total_pnl']:,.2f} -> "
          f"${modified['total_pnl']:,.2f} (${pnl_d:+,.2f})")
    print(f"    Max Drawdown:   {BASELINE['max_drawdown_pct']:.2f}% -> "
          f"{modified['max_drawdown_pct']:.2f}% ({dd_d:+.2f}%)")
    print(f"    Sharpe:         {BASELINE['sharpe_approx']:.2f} -> "
          f"{modified['sharpe_approx']:.2f} ({sh_d:+.2f})")
    print()

    # ────────────────────────────────────────────────────────────────
    #  DELIVERABLES
    # ────────────────────────────────────────────────────────────────
    d1 = "PASS" if st.total_trades > 0 else "FAIL"
    d2 = "PASS" if (LOGS_DIR / "full_modifier_backtest_results.json").exists() else "FAIL"
    d3 = "PASS"  # comparison table printed above
    d4 = "PASS" if (LOGS_DIR / "per_trade_modifier_log.json").exists() else "FAIL"
    d5 = "PASS"  # modifier activity summary printed above

    # Assessment: PF >= 1.3 and positive PnL
    d6 = ("PASS" if modified["profit_factor"] >= 1.3 and modified["total_pnl"] > 0
          else "REVIEW")

    print("=" * 75)
    print("  DELIVERABLES")
    print("=" * 75)
    print(f"  [1] Full backtest completed (all modifiers active):           [{d1}]")
    print(f"  [2] logs/full_modifier_backtest_results.json:                 [{d2}]")
    print(f"  [3] Comparison table (baseline vs modified vs delta):         [{d3}]")
    print(f"  [4] logs/per_trade_modifier_log.json (per-trade decisions):   [{d4}]")
    print(f"  [5] Modifier activity summary:                               [{d5}]")
    print(f"  [6] Assessment & recommendation:                             [{d6}]")
    print()

    # ── RECOMMENDATION ──
    print("  RECOMMENDATION:")
    if verdict == "IMPROVED":
        print("    Institutional modifiers IMPROVE performance. Keep enabled.")
    elif verdict == "MAINTAINED":
        print("    Institutional modifiers MAINTAIN performance within acceptable bounds.")
        print("    Keep enabled for risk management benefits (FOMC stand-aside,")
        print("    overnight position sizing).")
    else:
        print("    Institutional modifiers DEGRADE performance. Review thresholds")
        print("    or disable specific underperforming modifiers before going live.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
