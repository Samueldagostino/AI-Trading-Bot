"""
C2 Entry & Stop Forensic Analysis
==================================
Investigates why 48.8% of C2 trades exit at the initial stop and
25.6% exit at breakeven. Uses docs/viz_data.json (full backtest output)
which contains both trade records and OHLCV bars, enabling MFE/MAE
computation from raw price data.

ANALYSIS ONLY — no trading logic is modified.

Outputs:
  logs/c2_entry_stop_forensic.json   — machine-readable results
  logs/c2_entry_stop_summary.txt     — human-readable tables + recommendations
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIZ_DATA   = os.path.join(REPO_ROOT, "docs", "viz_data.json")
OUT_JSON   = os.path.join(REPO_ROOT, "logs", "c2_entry_stop_forensic.json")
OUT_TXT    = os.path.join(REPO_ROOT, "logs", "c2_entry_stop_summary.txt")

# ── Helpers ────────────────────────────────────────────────────────────────────

def safe_mean(vals):
    return round(sum(vals) / len(vals), 2) if vals else 0.0

def safe_pf(wins, losses):
    gross_win  = sum(v for v in wins  if v > 0)
    gross_loss = abs(sum(v for v in losses if v < 0))
    return round(gross_win / gross_loss, 2) if gross_loss else float("inf")

def fmt_pct(n, total):
    return f"{100*n/total:.1f}%" if total else "0.0%"

def load_data():
    if not os.path.exists(VIZ_DATA):
        sys.exit(f"ERROR: {VIZ_DATA} not found. Run the OOS validation first.")
    print(f"Loading {VIZ_DATA} ...")
    with open(VIZ_DATA, "r") as f:
        data = json.load(f)
    trades = data.get("trades", [])
    bars   = data.get("bars",   [])
    print(f"  Loaded {len(trades)} trades, {len(bars)} bars")
    return trades, bars

def build_bar_index(bars):
    """Index bars by bar_index (int) for O(1) lookup."""
    idx = {}
    for b in bars:
        bi = b.get("bar_index") or b.get("index")
        if bi is not None:
            idx[int(bi)] = b
    return idx

def compute_mfe_mae(trade, bar_idx):
    """
    Compute MFE and MAE for a trade using raw OHLCV bars.
    MFE = max excursion in the trade direction (profit side).
    MAE = max excursion against the trade direction (loss side).
    Returns (mfe_pts, mae_pts, bars_scanned).
    """
    direction   = trade.get("direction", "").lower()
    entry_price = trade.get("entry_price") or trade.get("c1_entry_price")
    entry_bar   = trade.get("entry_bar")
    exit_bar    = trade.get("exit_bar")

    if not all([direction, entry_price, entry_bar is not None, exit_bar is not None]):
        return None, None, 0

    entry_bar = int(entry_bar)
    exit_bar  = int(exit_bar)

    highs, lows = [], []
    for bi in range(entry_bar, exit_bar + 1):
        b = bar_idx.get(bi)
        if b:
            highs.append(b.get("high", entry_price))
            lows.append(b.get("low",  entry_price))

    if not highs:
        return None, None, 0

    max_high = max(highs)
    min_low  = min(lows)

    if direction == "long":
        mfe = max_high - entry_price
        mae = entry_price - min_low
    else:  # short
        mfe = entry_price - min_low
        mae = max_high - entry_price

    return round(mfe, 2), round(mae, 2), len(highs)

def bars_after_exit(trade, bar_idx, n=30):
    """
    Look at n bars after exit to check for continuation.
    For a stopped-out trade, did price continue past the stop?
    Returns points continued in original trade direction.
    """
    direction = trade.get("direction", "").lower()
    exit_bar  = trade.get("exit_bar")
    stop_price = trade.get("stop_price") or trade.get("initial_stop")

    if exit_bar is None or not stop_price:
        return None

    exit_bar = int(exit_bar)
    prices_after = []
    for bi in range(exit_bar + 1, exit_bar + n + 1):
        b = bar_idx.get(bi)
        if b:
            prices_after.append(b.get("low" if direction == "long" else "high", stop_price))

    if not prices_after:
        return None

    if direction == "long":
        # How far below the stop did price go? (continued down = stop was hunted)
        # Actually for LONG stopped out: we hit stop going down.
        # Continuation = how much further down after stop
        min_after = min(prices_after)
        return round(stop_price - min_after, 2)  # positive = continued down past stop
    else:
        max_after = max(prices_after)
        return round(max_after - stop_price, 2)   # positive = continued up past stop

# ── Main Analysis ──────────────────────────────────────────────────────────────

def run():
    trades, bars = load_data()
    bar_idx = build_bar_index(bars)

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)

    # ── Enrich each trade with MFE/MAE ────────────────────────────────────────
    print("Computing MFE/MAE from bar data ...")
    enriched = []
    mfe_computed = 0
    for t in trades:
        mfe, mae, n_bars = compute_mfe_mae(t, bar_idx)
        rec = dict(t)
        rec["mfe_pts"]      = mfe
        rec["mae_pts"]      = mae
        rec["bars_scanned"] = n_bars
        if mfe is not None:
            mfe_computed += 1
        enriched.append(rec)
    print(f"  MFE/MAE computed for {mfe_computed}/{len(enriched)} trades")

    # Separate C2-specific trades
    c2_trades = [t for t in enriched if t.get("c2_exit_reason")]
    total_c2  = len(c2_trades)
    print(f"  C2 trades found: {total_c2}")

    # ── INVESTIGATION A: Stop Placement Quality ────────────────────────────────
    print("\nRunning Investigation A: Stop Placement Quality ...")

    stopped = [t for t in c2_trades if t.get("c2_exit_reason") in ("stop", "initial_stop")]
    n_stopped = len(stopped)
    print(f"  C2 stops: {n_stopped} ({fmt_pct(n_stopped, total_c2)} of C2 trades)")

    bucket_A, bucket_B, bucket_C, bucket_D = [], [], [], []
    no_mfe_count = 0

    for t in stopped:
        mfe = t.get("mfe_pts")
        c2_pnl = t.get("c2_pnl", 0)

        if mfe is None:
            no_mfe_count += 1
            continue

        # Check for stop hunt: did price continue >20pts past the stop?
        cont = bars_after_exit(t, bar_idx, n=30)
        is_hunted = cont is not None and cont > 20

        if is_hunted:
            bucket_D.append({"c2_pnl": c2_pnl, "mfe": mfe, "continuation": cont})
        elif mfe < 5:
            bucket_A.append({"c2_pnl": c2_pnl, "mfe": mfe})
        elif mfe <= 15:
            bucket_B.append({"c2_pnl": c2_pnl, "mfe": mfe})
        else:
            bucket_C.append({"c2_pnl": c2_pnl, "mfe": mfe})

    inv_a = {
        "total_stopped":   n_stopped,
        "no_mfe_data":     no_mfe_count,
        "bucket_A_bad_entry": {
            "label":       "MFE < 5pts — trade never worked (bad entry)",
            "count":       len(bucket_A),
            "pct":         fmt_pct(len(bucket_A), n_stopped),
            "avg_c2_pnl":  safe_mean([x["c2_pnl"] for x in bucket_A]),
            "avg_mfe":     safe_mean([x["mfe"]    for x in bucket_A]),
        },
        "bucket_B_okay_entry": {
            "label":       "MFE 5-15pts — worked briefly then reversed",
            "count":       len(bucket_B),
            "pct":         fmt_pct(len(bucket_B), n_stopped),
            "avg_c2_pnl":  safe_mean([x["c2_pnl"] for x in bucket_B]),
            "avg_mfe":     safe_mean([x["mfe"]    for x in bucket_B]),
        },
        "bucket_C_stop_too_tight": {
            "label":       "MFE > 15pts — trade was working, stop too tight",
            "count":       len(bucket_C),
            "pct":         fmt_pct(len(bucket_C), n_stopped),
            "avg_c2_pnl":  safe_mean([x["c2_pnl"] for x in bucket_C]),
            "avg_mfe":     safe_mean([x["mfe"]    for x in bucket_C]),
        },
        "bucket_D_stop_hunted": {
            "label":       "Continuation >20pts past stop — stop got hunted",
            "count":       len(bucket_D),
            "pct":         fmt_pct(len(bucket_D), n_stopped),
            "avg_c2_pnl":  safe_mean([x["c2_pnl"]       for x in bucket_D]),
            "avg_mfe":     safe_mean([x["mfe"]           for x in bucket_D]),
            "avg_continuation": safe_mean([x["continuation"] for x in bucket_D]),
        },
    }

    # ── INVESTIGATION B: Breakeven Exit Analysis ───────────────────────────────
    print("Running Investigation B: Breakeven Exit Analysis ...")

    be_trades = [t for t in c2_trades if t.get("c2_exit_reason") in ("breakeven", "be")]
    n_be = len(be_trades)
    print(f"  C2 breakeven exits: {n_be} ({fmt_pct(n_be, total_c2)} of C2 trades)")

    be_with_mfe = [t for t in be_trades if t.get("mfe_pts") is not None]
    stolen_runners = []   # BE exits that had MFE > 20pts after exit

    for t in be_trades:
        mfe = t.get("mfe_pts")
        if mfe is None:
            continue
        # Check if price ran further after the breakeven exit
        cont = bars_after_exit(t, bar_idx, n=60)
        if cont is not None and cont > 20:
            stolen_runners.append({
                "mfe_at_be":    mfe,
                "run_after_be": cont,
                "c1_pnl":       t.get("c1_pnl", 0),
                "c2_pnl":       t.get("c2_pnl", 0),
            })

    # Counterfactual: if C2 was NOT moved to BE, what would avg C2 PnL be?
    # Use the stopped-out C2 trades avg stop_distance as the "would have been stopped" scenario
    avg_stop_dist = safe_mean([t.get("stop_distance", 0) for t in c2_trades if t.get("c2_exit_reason") == "stop"])
    # For BE trades that later ran, C2 would have profited from the run
    counterfactual_gain_per_stolen = safe_mean([x["run_after_be"] for x in stolen_runners])

    inv_b = {
        "total_be_exits":    n_be,
        "pct_of_c2_trades":  fmt_pct(n_be, total_c2),
        "avg_c2_pnl_at_be":  safe_mean([t.get("c2_pnl", 0) for t in be_trades]),
        "avg_c1_pnl_at_be":  safe_mean([t.get("c1_pnl", 0) for t in be_trades]),
        "mfe_data_available": len(be_with_mfe),
        "avg_mfe_before_be": safe_mean([t["mfe_pts"] for t in be_with_mfe]),
        "stolen_runners": {
            "count":       len(stolen_runners),
            "pct_of_be":   fmt_pct(len(stolen_runners), n_be),
            "avg_run_after_be_pts":       counterfactual_gain_per_stolen,
            "lost_opportunity_per_trade": counterfactual_gain_per_stolen,
            "total_lost_opportunity_pts": round(counterfactual_gain_per_stolen * len(stolen_runners), 2),
            "total_lost_opportunity_usd": round(counterfactual_gain_per_stolen * len(stolen_runners) * 2, 2),
        },
        "verdict": (
            "BE mechanism is likely stealing runners"
            if len(stolen_runners) > n_be * 0.3
            else "BE mechanism impact appears limited"
        ),
    }

    # ── INVESTIGATION C: Entry Timing ─────────────────────────────────────────
    print("Running Investigation C: Entry Timing ...")

    # For each trade, estimate how much of the potential move already happened.
    # Proxy: stop_distance vs signal_score. Tight stop + high score = early entry.
    # We group by stop_distance thirds (narrow, medium, wide) as a proxy for entry timing.
    stop_dists = [t.get("stop_distance", 0) for t in c2_trades if t.get("stop_distance")]
    if stop_dists:
        stop_dists_sorted = sorted(stop_dists)
        n = len(stop_dists_sorted)
        t1 = stop_dists_sorted[n // 3]
        t2 = stop_dists_sorted[2 * n // 3]
    else:
        t1, t2 = 15, 25

    def stop_group(t):
        sd = t.get("stop_distance", 0)
        if sd <= t1:  return "tight_stop"
        if sd <= t2:  return "medium_stop"
        return "wide_stop"

    group_data = defaultdict(list)
    for t in c2_trades:
        g = stop_group(t)
        group_data[g].append(t.get("c2_pnl", 0))

    # Also group by signal_score thirds
    scores = [t.get("signal_score", 0) for t in c2_trades if t.get("signal_score")]
    if scores:
        scores_sorted = sorted(scores)
        s1 = scores_sorted[len(scores_sorted) // 3]
        s2 = scores_sorted[2 * len(scores_sorted) // 3]
    else:
        s1, s2 = 0.75, 0.82

    score_data = defaultdict(list)
    for t in c2_trades:
        sc = t.get("signal_score", 0)
        if sc <= s1:   score_data["low_score_0.75"].append(t.get("c2_pnl", 0))
        elif sc <= s2: score_data["med_score"].append(t.get("c2_pnl", 0))
        else:          score_data["high_score"].append(t.get("c2_pnl", 0))

    # Signal source breakdown
    source_data = defaultdict(list)
    for t in c2_trades:
        src = t.get("signal_source", "unknown")
        source_data[src].append(t.get("c2_pnl", 0))

    def group_stats(pnl_list):
        wins   = [v for v in pnl_list if v > 0]
        losses = [v for v in pnl_list if v <= 0]
        return {
            "count":    len(pnl_list),
            "avg_pnl":  safe_mean(pnl_list),
            "win_rate": fmt_pct(len(wins), len(pnl_list)),
            "pf":       safe_pf(wins, losses),
            "total_pnl": round(sum(pnl_list), 2),
        }

    inv_c = {
        "stop_distance_thresholds": {"tight": f"<={t1}pts", "medium": f"{t1}-{t2}pts", "wide": f">{t2}pts"},
        "by_stop_distance": {
            "tight_stop":  group_stats(group_data["tight_stop"]),
            "medium_stop": group_stats(group_data["medium_stop"]),
            "wide_stop":   group_stats(group_data["wide_stop"]),
        },
        "signal_score_thresholds": {"low": f"0.75-{s1:.2f}", "med": f"{s1:.2f}-{s2:.2f}", "high": f">{s2:.2f}"},
        "by_signal_score": {
            "low_score":  group_stats(score_data["low_score_0.75"]),
            "med_score":  group_stats(score_data["med_score"]),
            "high_score": group_stats(score_data["high_score"]),
        },
        "by_signal_source": {
            src: group_stats(pnls) for src, pnls in source_data.items()
        },
    }

    # ── INVESTIGATION D: Recommendations ──────────────────────────────────────
    print("Generating Investigation D: Recommendations ...")

    # Score each intervention by: trades_affected × avg_pnl_impact
    # Dollar impact estimate: 1pt = $2 MNQ

    recs = []

    # Rec 1: Stop hunt problem (Bucket D)
    if bucket_D:
        trades_affected = len(bucket_D)
        avg_cont = safe_mean([x["continuation"] for x in bucket_D])
        dollar_impact = round(trades_affected * avg_cont * 2, 0)
        recs.append({
            "rank": None,
            "recommendation": "Use structural stop placement (below sweep lows) instead of ATR-fixed stops",
            "finding": f"Bucket D: {len(bucket_D)} trades ({fmt_pct(len(bucket_D), n_stopped)} of stops) had price continue >20pts past the stop",
            "trades_affected": trades_affected,
            "avg_pts_recoverable": avg_cont,
            "estimated_usd_impact": dollar_impact,
            "priority": "HIGH" if len(bucket_D) > n_stopped * 0.2 else "MEDIUM",
        })

    # Rec 2: Stop too tight (Bucket C)
    if bucket_C:
        trades_affected = len(bucket_C)
        avg_mfe = safe_mean([x["mfe"] for x in bucket_C])
        dollar_impact = round(trades_affected * avg_mfe * 0.5 * 2, 0)  # recover 50% of MFE
        recs.append({
            "rank": None,
            "recommendation": "Widen C2 initial stop by 20-30% for trades with MFE >15pts that still stopped out",
            "finding": f"Bucket C: {len(bucket_C)} C2 trades reached MFE >15pts but still hit the stop",
            "trades_affected": trades_affected,
            "avg_pts_recoverable": round(avg_mfe * 0.5, 2),
            "estimated_usd_impact": dollar_impact,
            "priority": "HIGH" if len(bucket_C) > n_stopped * 0.25 else "MEDIUM",
        })

    # Rec 3: Breakeven mechanism stealing runners
    if stolen_runners:
        trades_affected = len(stolen_runners)
        dollar_impact = round(inv_b["stolen_runners"]["total_lost_opportunity_usd"], 0)
        recs.append({
            "rank": None,
            "recommendation": "Delay breakeven trigger or use a wider breakeven offset for C2",
            "finding": f"{len(stolen_runners)} BE exits were followed by runs >20pts — BE mechanism exiting too early",
            "trades_affected": trades_affected,
            "avg_pts_recoverable": counterfactual_gain_per_stolen,
            "estimated_usd_impact": dollar_impact,
            "priority": "HIGH" if len(stolen_runners) > n_be * 0.3 else "MEDIUM",
        })

    # Rec 4: Bad entry quality (Bucket A)
    if bucket_A:
        trades_affected = len(bucket_A)
        avg_stop = safe_mean([t.get("stop_distance", 0) for t in stopped if t.get("mfe_pts", 0) < 5])
        dollar_impact = round(trades_affected * avg_stop * 2 * 0.5, 0)  # avoid 50% of these
        recs.append({
            "rank": None,
            "recommendation": "Add MFE >3pt minimum threshold to C2 entry — trades with MFE <5pts never worked",
            "finding": f"Bucket A: {len(bucket_A)} C2 trades ({fmt_pct(len(bucket_A), n_stopped)} of stops) had MFE <5pts — bad entry quality",
            "trades_affected": trades_affected,
            "avg_pts_recoverable": round(avg_stop * 0.5, 2),
            "estimated_usd_impact": dollar_impact,
            "priority": "MEDIUM",
        })

    # Rec 5: Signal score / entry timing
    low_score_stats  = group_stats(score_data["low_score_0.75"])
    high_score_stats = group_stats(score_data["high_score"])
    if low_score_stats["avg_pnl"] < 0 and high_score_stats["avg_pnl"] > 0:
        trades_affected = low_score_stats["count"]
        dollar_impact = round(abs(low_score_stats["total_pnl"]) * 0.5, 0)
        recs.append({
            "rank": None,
            "recommendation": "Raise HC min score for C2 entry — low-score signals have negative C2 expectancy",
            "finding": f"Low-score C2 trades: avg PnL ${low_score_stats['avg_pnl']} vs high-score ${high_score_stats['avg_pnl']}",
            "trades_affected": trades_affected,
            "avg_pts_recoverable": round(abs(low_score_stats["avg_pnl"]) / 2, 2),
            "estimated_usd_impact": dollar_impact,
            "priority": "MEDIUM",
        })

    # Sort by dollar impact descending
    recs.sort(key=lambda x: x["estimated_usd_impact"], reverse=True)
    for i, r in enumerate(recs):
        r["rank"] = i + 1

    # ── Overall C2 Summary ─────────────────────────────────────────────────────
    c2_by_reason = defaultdict(list)
    for t in c2_trades:
        c2_by_reason[t.get("c2_exit_reason", "unknown")].append(t.get("c2_pnl", 0))

    c2_summary = {
        "total_c2_trades": total_c2,
        "by_exit_reason": {},
    }
    for reason, pnls in sorted(c2_by_reason.items()):
        wins   = [v for v in pnls if v > 0]
        losses = [v for v in pnls if v <= 0]
        c2_summary["by_exit_reason"][reason] = {
            "count":     len(pnls),
            "pct":       fmt_pct(len(pnls), total_c2),
            "avg_pnl":   safe_mean(pnls),
            "total_pnl": round(sum(pnls), 2),
            "win_rate":  fmt_pct(len(wins), len(pnls)),
            "pf":        safe_pf(wins, losses),
        }

    # ── Assemble output ────────────────────────────────────────────────────────
    output = {
        "generated":         datetime.now().isoformat(),
        "source_file":       VIZ_DATA,
        "total_trades":      len(enriched),
        "total_c2_trades":   total_c2,
        "mfe_computed_count": mfe_computed,
        "c2_exit_reason_summary": c2_summary,
        "investigation_A_stop_placement":   inv_a,
        "investigation_B_breakeven_steal":  inv_b,
        "investigation_C_entry_timing":     inv_c,
        "investigation_D_recommendations":  recs,
    }

    with open(OUT_JSON, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {OUT_JSON}")

    # ── Human-readable summary ─────────────────────────────────────────────────
    lines = []
    def p(*args):
        lines.append(" ".join(str(a) for a in args))
    def sep(char="─", n=72):
        lines.append(char * n)
    def h(title):
        sep("═")
        lines.append(f"  {title}")
        sep("═")

    h("C2 ENTRY & STOP FORENSIC ANALYSIS")
    p(f"Generated:    {output['generated']}")
    p(f"Source:       {VIZ_DATA}")
    p(f"Total trades: {len(enriched)}  |  C2 trades: {total_c2}  |  MFE computed: {mfe_computed}")

    h("C2 EXIT REASON DISTRIBUTION")
    p(f"{'Exit Reason':<20} {'Count':>6} {'Pct':>7} {'Avg PnL':>9} {'Total PnL':>11} {'WR':>7} {'PF':>6}")
    sep()
    for reason, s in c2_summary["by_exit_reason"].items():
        p(f"{reason:<20} {s['count']:>6} {s['pct']:>7} {s['avg_pnl']:>9.2f} {s['total_pnl']:>11.2f} {s['win_rate']:>7} {s['pf']:>6}")

    h("INVESTIGATION A — STOP PLACEMENT QUALITY")
    p(f"C2 trades stopped out: {n_stopped} ({fmt_pct(n_stopped, total_c2)} of all C2 trades)")
    p()
    for bk, bd in [
        ("A — Bad entry (MFE <5pts)",  inv_a["bucket_A_bad_entry"]),
        ("B — Worked briefly (5-15)",  inv_a["bucket_B_okay_entry"]),
        ("C — Stop too tight (>15)",   inv_a["bucket_C_stop_too_tight"]),
        ("D — Stop hunted (>20pt cont)", inv_a["bucket_D_stop_hunted"]),
    ]:
        p(f"  Bucket {bk}")
        p(f"    Count: {bd['count']}  ({bd['pct']})  |  Avg C2 PnL: ${bd['avg_c2_pnl']}  |  Avg MFE: {bd['avg_mfe']}pts")
        if "avg_continuation" in bd:
            p(f"    Avg continuation past stop: {bd['avg_continuation']}pts")
        p()
    p(f"  (No MFE data for {inv_a['no_mfe_data']} stopped trades — bar data missing for those bars)")

    h("INVESTIGATION B — BREAKEVEN EXIT ANALYSIS")
    p(f"C2 breakeven exits: {n_be} ({fmt_pct(n_be, total_c2)} of all C2 trades)")
    p(f"Avg C2 PnL at breakeven: ${inv_b['avg_c2_pnl_at_be']}")
    p(f"Avg C1 PnL for those trades: ${inv_b['avg_c1_pnl_at_be']}")
    p(f"Avg MFE before BE trigger: {inv_b['avg_mfe_before_be']}pts")
    p()
    p(f"STOLEN RUNNERS (BE exit followed by >20pt run):")
    sr = inv_b["stolen_runners"]
    p(f"  Count:             {sr['count']} ({sr['pct_of_be']} of BE exits)")
    p(f"  Avg run after BE:  {sr['avg_run_after_be_pts']}pts")
    p(f"  Lost opportunity:  {sr['total_lost_opportunity_pts']}pts  =  ${sr['total_lost_opportunity_usd']} USD")
    p(f"  VERDICT: {inv_b['verdict']}")

    h("INVESTIGATION C — ENTRY TIMING (Stop Distance & Score Proxies)")
    p("Stop distance as proxy for entry quality (narrow = tighter risk = cleaner entry):")
    p(f"  Thresholds: {inv_c['stop_distance_thresholds']}")
    p()
    p(f"  {'Group':<15} {'Count':>6} {'WR':>7} {'Avg PnL':>9} {'Total PnL':>11} {'PF':>6}")
    sep()
    for g, s in inv_c["by_stop_distance"].items():
        p(f"  {g:<15} {s['count']:>6} {s['win_rate']:>7} {s['avg_pnl']:>9.2f} {s['total_pnl']:>11.2f} {s['pf']:>6}")
    p()
    p("Signal score breakdown:")
    p(f"  {'Score Group':<15} {'Count':>6} {'WR':>7} {'Avg PnL':>9} {'Total PnL':>11} {'PF':>6}")
    sep()
    for g, s in inv_c["by_signal_score"].items():
        p(f"  {g:<15} {s['count']:>6} {s['win_rate']:>7} {s['avg_pnl']:>9.2f} {s['total_pnl']:>11.2f} {s['pf']:>6}")
    p()
    p("By signal source:")
    p(f"  {'Source':<20} {'Count':>6} {'WR':>7} {'Avg PnL':>9} {'Total PnL':>11} {'PF':>6}")
    sep()
    for src, s in inv_c["by_signal_source"].items():
        p(f"  {src:<20} {s['count']:>6} {s['win_rate']:>7} {s['avg_pnl']:>9.2f} {s['total_pnl']:>11.2f} {s['pf']:>6}")

    h("INVESTIGATION D — RANKED RECOMMENDATIONS")
    if recs:
        for r in recs:
            p(f"  #{r['rank']}  [{r['priority']}]  {r['recommendation']}")
            p(f"      Finding:          {r['finding']}")
            p(f"      Trades affected:  {r['trades_affected']}")
            p(f"      Pts recoverable:  {r['avg_pts_recoverable']}pts avg")
            p(f"      Est. USD impact:  ${r['estimated_usd_impact']:,.0f}")
            p()
    else:
        p("  No significant recommendations generated — findings inconclusive.")
        p("  Consider running with a dataset that includes more C2 breakeven exits.")

    h("DATA GAPS & CAVEATS")
    p("  - MFE/MAE derived from bar OHLCV data (close approximation, not tick-exact)")
    p("  - 'Stop hunted' detection uses 30-bar lookforward — some false positives likely")
    p("  - Entry timing analysis uses stop_distance as proxy (no direct sweep-level data)")
    p("  - All recommendations are ANALYSIS ONLY — validate with full backtest before implementing")

    summary_text = "\n".join(lines)
    print("\n" + summary_text)

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write(summary_text)
    print(f"\nWrote {OUT_TXT}")

    return output

if __name__ == "__main__":
    results = run()
    print("\n✓ Analysis complete.")
    print(f"  JSON: {OUT_JSON}")
    print(f"  TXT:  {OUT_TXT}")
