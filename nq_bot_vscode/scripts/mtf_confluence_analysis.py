#!/usr/bin/env python3
"""
MTF Confluence Analysis
========================
Phase 1: Run 4 HTF configs (A=no HTF, B/C/D = strength gates 0.7/0.5/0.3)
Phase 2: Kill/save matrix — classify each Config A trade vs HTF gates
Phase 3: Regime + time-of-day + HTF cross-analysis on best config
Phase 4: Generate markdown report

Pure analysis — no code changes, no HC filter modifications.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncio
import json
import copy
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from config.settings import CONFIG
from features.engine import Bar
from features.htf_engine import HTFBiasEngine, HTFBar, HTFBiasResult
from data_pipeline.pipeline import DataPipeline, bardata_to_bar, bardata_to_htfbar, BarData
from main import TradingOrchestrator, HTF_TIMEFRAMES, EXECUTION_TIMEFRAMES

logging.basicConfig(
    level=logging.WARNING,  # Quiet — we only want our summary output
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)


# ══════════════════════════════════════════════════════════════
#  PHASE 1 — Run 4 configurations
# ══════════════════════════════════════════════════════════════

async def run_config_a() -> Tuple[dict, list]:
    """Config A: Single-TF, no HTF engine. HC filter ON."""
    pipeline = DataPipeline(CONFIG)
    bar_data = pipeline.tv_importer.import_file(
        os.path.join(os.path.dirname(__file__), '..', 'data', 'tradingview')
        + '/CME_MINI_MNQ1!, 2m.csv'
    )
    bars = pipeline.convert_to_feature_bars(bar_data)

    bot = TradingOrchestrator(CONFIG)
    await bot.initialize(skip_db=True)

    trades = []  # Collect per-trade detail
    for bar in bars:
        result = await bot.process_bar(bar)
        if result:
            # Attach bar timestamp for time-of-day analysis
            result['_bar_timestamp'] = bar.timestamp
            trades.append(result)

    stats = bot.executor.get_stats()
    risk = bot.risk_engine.get_state_snapshot()
    sig_stats = bot.signal_aggregator.get_signal_stats()

    summary = {
        'total_trades': stats.get('total_trades', 0),
        'total_pnl': round(stats.get('total_pnl', 0), 2),
        'win_rate': stats.get('win_rate', 0),
        'profit_factor': stats.get('profit_factor', 0),
        'avg_winner': stats.get('avg_winner', 0),
        'avg_loser': stats.get('avg_loser', 0),
        'largest_win': stats.get('largest_win', 0),
        'largest_loss': stats.get('largest_loss', 0),
        'max_drawdown_pct': risk.get('max_drawdown_pct', 0),
        'htf_blocked': 0,
        'c1_pnl': round(stats.get('c1_total_pnl', 0), 2),
        'c2_pnl': round(stats.get('c2_total_pnl', 0), 2),
    }
    return summary, trades


async def run_config_mtf(strength_gate: float, label: str) -> Tuple[dict, list]:
    """
    Config B/C/D: Full MTF backtest with variable HTF strength gate.

    Monkey-patches HTFBiasEngine.get_bias to use the specified threshold.
    """
    # Monkey-patch the strength gate
    original_get_bias = HTFBiasEngine.get_bias

    def patched_get_bias(self_engine, timestamp=None):
        bullish = 0
        bearish = 0
        total = 0
        for tf, bias in self_engine._biases.items():
            total += 1
            if bias == "bullish":
                bullish += 1
            elif bias == "bearish":
                bearish += 1

        if total == 0:
            return HTFBiasResult(timestamp=timestamp)

        strength = max(bullish, bearish) / total
        if bullish > bearish:
            direction = "bullish"
        elif bearish > bullish:
            direction = "bearish"
        else:
            direction = "neutral"

        result = HTFBiasResult(
            consensus_direction=direction,
            consensus_strength=round(strength, 3),
            htf_allows_long=(direction != "bearish" or strength < strength_gate),
            htf_allows_short=(direction != "bullish" or strength < strength_gate),
            timestamp=timestamp,
            tf_biases=dict(self_engine._biases),
        )
        self_engine._last_result = result
        return result

    HTFBiasEngine.get_bias = patched_get_bias

    try:
        pipeline = DataPipeline(CONFIG)
        tf_bars = pipeline.load_mtf_data(
            os.path.join(os.path.dirname(__file__), '..', 'data', 'tradingview')
        )
        mtf_iterator = pipeline.create_mtf_iterator(tf_bars)

        bot = TradingOrchestrator(CONFIG)
        await bot.initialize(skip_db=True)

        # Run MTF backtest, collecting per-trade details
        execution_tf = "2m"
        bot._execution_tf = execution_tf
        trades = []

        for timeframe, bar_data in mtf_iterator:
            if timeframe in HTF_TIMEFRAMES:
                bot.process_htf_bar(timeframe, bar_data)
            elif timeframe == execution_tf:
                exec_bar = bardata_to_bar(bar_data)
                result = await bot.process_bar(exec_bar)
                if result:
                    result['_bar_timestamp'] = bar_data.timestamp
                    # Capture HTF state at this moment
                    htf = bot._htf_bias
                    if htf:
                        result['_htf_direction'] = htf.consensus_direction
                        result['_htf_strength'] = htf.consensus_strength
                        result['_htf_allows_long'] = htf.htf_allows_long
                        result['_htf_allows_short'] = htf.htf_allows_short
                        result['_htf_tf_biases'] = dict(htf.tf_biases)
                    trades.append(result)

        stats = bot.executor.get_stats()
        risk = bot.risk_engine.get_state_snapshot()
        sig_stats = bot.signal_aggregator.get_signal_stats()

        summary = {
            'total_trades': stats.get('total_trades', 0),
            'total_pnl': round(stats.get('total_pnl', 0), 2),
            'win_rate': stats.get('win_rate', 0),
            'profit_factor': stats.get('profit_factor', 0),
            'avg_winner': stats.get('avg_winner', 0),
            'avg_loser': stats.get('avg_loser', 0),
            'largest_win': stats.get('largest_win', 0),
            'largest_loss': stats.get('largest_loss', 0),
            'max_drawdown_pct': risk.get('max_drawdown_pct', 0),
            'htf_blocked': sig_stats.get('htf_blocked_signals', 0),
            'c1_pnl': round(stats.get('c1_total_pnl', 0), 2),
            'c2_pnl': round(stats.get('c2_total_pnl', 0), 2),
        }
        return summary, trades

    finally:
        HTFBiasEngine.get_bias = original_get_bias


# ══════════════════════════════════════════════════════════════
#  PHASE 2 — Kill/Save Matrix
# ══════════════════════════════════════════════════════════════

async def build_htf_replay_engine():
    """
    Replay all bars to build an HTF engine state timeline.
    Returns a function that, given a timestamp + direction, says
    what the HTF engine would have allowed at each strength gate.
    """
    pipeline = DataPipeline(CONFIG)
    tf_bars = pipeline.load_mtf_data(
        os.path.join(os.path.dirname(__file__), '..', 'data', 'tradingview')
    )
    mtf_iterator = pipeline.create_mtf_iterator(tf_bars)

    # Replay HTF bars and record state at each 2m bar timestamp
    engine = HTFBiasEngine(config=CONFIG, timeframes=list(HTF_TIMEFRAMES))
    htf_snapshots = {}  # timestamp -> {direction, strength, tf_biases}

    for timeframe, bar_data in mtf_iterator:
        if timeframe in HTF_TIMEFRAMES:
            htf_bar = bardata_to_htfbar(bar_data)
            engine.update_bar(timeframe, htf_bar)
        elif timeframe == "2m":
            # Snapshot HTF state at each 2m bar
            bias = engine.get_bias(bar_data.timestamp)
            htf_snapshots[bar_data.timestamp] = {
                'direction': bias.consensus_direction,
                'strength': bias.consensus_strength,
                'tf_biases': dict(bias.tf_biases),
            }

    return htf_snapshots


def would_htf_allow(htf_snap: dict, trade_direction: str, gate: float) -> bool:
    """Check if HTF engine at a given strength gate would allow this trade."""
    if htf_snap is None:
        return True  # No HTF data = allow

    direction = htf_snap['direction']
    strength = htf_snap['strength']

    if trade_direction == "long":
        return direction != "bearish" or strength < gate
    elif trade_direction == "short":
        return direction != "bullish" or strength < gate
    return True


def compute_kill_save_matrix(config_a_trades: list, htf_snapshots: dict, gate: float) -> dict:
    """
    Classify each Config A trade against an HTF gate.
    Returns counts for TP, TN, FP, FN.
    """
    tp = 0  # Winning trade, HTF allows (correct)
    tn = 0  # Losing trade, HTF blocks (correct)
    fp = 0  # Losing trade, HTF allows (failure)
    fn = 0  # Winning trade, HTF blocks (cost)

    tp_pnl = 0.0
    tn_pnl = 0.0
    fp_pnl = 0.0
    fn_pnl = 0.0

    for trade in config_a_trades:
        if trade.get('action') != 'trade_closed':
            continue

        pnl = trade['total_pnl']
        is_winner = pnl > 0
        direction = trade['direction']
        ts = trade.get('_bar_timestamp')

        # Find the closest HTF snapshot
        htf_snap = htf_snapshots.get(ts)
        if htf_snap is None:
            # Try to find nearest snapshot
            if htf_snapshots:
                closest_ts = min(htf_snapshots.keys(), key=lambda t: abs((t - ts).total_seconds()) if ts else float('inf'))
                if abs((closest_ts - ts).total_seconds()) < 300:  # within 5 min
                    htf_snap = htf_snapshots[closest_ts]

        allowed = would_htf_allow(htf_snap, direction, gate)

        if is_winner and allowed:
            tp += 1
            tp_pnl += pnl
        elif not is_winner and not allowed:
            tn += 1
            tn_pnl += pnl  # This is the PnL saved (negative number we'd avoid)
        elif not is_winner and allowed:
            fp += 1
            fp_pnl += pnl
        elif is_winner and not allowed:
            fn += 1
            fn_pnl += pnl

    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    return {
        'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn,
        'tp_pnl': round(tp_pnl, 2),
        'tn_pnl': round(tn_pnl, 2),  # PnL of blocked losers (saved losses)
        'fp_pnl': round(fp_pnl, 2),  # PnL of allowed losers (failures)
        'fn_pnl': round(fn_pnl, 2),  # PnL of blocked winners (missed gains)
        'precision': round(precision, 4),
        'recall': round(recall, 4),
        'f1': round(f1, 4),
        'total': total,
        'pnl_saved': round(abs(tn_pnl), 2),  # How much loss avoided
        'pnl_sacrificed': round(fn_pnl, 2),    # How much gain forfeited
        'net_filter_value': round(abs(tn_pnl) - fn_pnl, 2),  # Positive = filter helps
    }


# ══════════════════════════════════════════════════════════════
#  PHASE 3 — Cross-Analysis
# ══════════════════════════════════════════════════════════════

def get_session_bucket(ts: datetime) -> str:
    """Classify timestamp into trading session bucket (ET)."""
    et_time_obj = ts.astimezone(ZoneInfo("America/New_York"))
    et_hour = et_time_obj.hour
    et_minute = et_time_obj.minute

    time_decimal = et_hour + et_minute / 60.0

    if 6.0 <= time_decimal < 9.5:
        return "pre-market (6-9:30)"
    elif 9.5 <= time_decimal < 11.5:
        return "morning (9:30-11:30)"
    elif 11.5 <= time_decimal < 14.0:
        return "midday (11:30-14:00)"
    elif 14.0 <= time_decimal < 16.0:
        return "afternoon (14-16:00)"
    else:
        return "overnight/extended"


def analyze_cross_dimensions(trades: list, htf_snapshots: dict = None) -> dict:
    """Break down trades by regime, session, and HTF direction."""

    # Only look at closed trades
    closed = [t for t in trades if t.get('action') == 'trade_closed']

    results = {
        'by_regime': defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0, 'winners_pnl': 0.0, 'losers_pnl': 0.0}),
        'by_session': defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0, 'winners_pnl': 0.0, 'losers_pnl': 0.0}),
        'by_htf_direction': defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0, 'winners_pnl': 0.0, 'losers_pnl': 0.0}),
        'combined_filters': [],
    }

    # Find entry events for each trade to get timestamps and context
    entries = {t.get('trade_id'): t for t in trades if t.get('action') == 'entry'}

    for trade in closed:
        pnl = trade['total_pnl']
        is_win = pnl > 0
        regime = trade.get('regime', 'unknown')
        trade_id = trade.get('trade_id')

        # Get entry info
        entry = entries.get(trade_id, {})
        ts = entry.get('_bar_timestamp') or trade.get('_bar_timestamp')

        session = get_session_bucket(ts) if ts else "unknown"

        # Get HTF direction at entry
        htf_dir = entry.get('_htf_direction', 'n/a')
        if htf_dir == 'n/a' and ts and htf_snapshots:
            snap = htf_snapshots.get(ts)
            if snap:
                htf_dir = snap['direction']

        # Regime
        r = results['by_regime'][regime]
        r['trades'] += 1
        r['pnl'] += pnl
        if is_win:
            r['wins'] += 1
            r['winners_pnl'] += pnl
        else:
            r['losers_pnl'] += pnl

        # Session
        s = results['by_session'][session]
        s['trades'] += 1
        s['pnl'] += pnl
        if is_win:
            s['wins'] += 1
            s['winners_pnl'] += pnl
        else:
            s['losers_pnl'] += pnl

        # HTF direction
        h = results['by_htf_direction'][htf_dir]
        h['trades'] += 1
        h['pnl'] += pnl
        if is_win:
            h['wins'] += 1
            h['winners_pnl'] += pnl
        else:
            h['losers_pnl'] += pnl

        # Track combined for later filter analysis
        results['combined_filters'].append({
            'trade_id': trade_id,
            'pnl': pnl,
            'is_win': is_win,
            'regime': regime,
            'session': session,
            'htf_dir': htf_dir,
            'direction': trade['direction'],
        })

    return results


def find_combined_filters(combined_data: list) -> list:
    """
    Try every combination of regime + session + HTF filters
    to find which combo flips February positive.
    """
    regimes = set(t['regime'] for t in combined_data)
    sessions = set(t['session'] for t in combined_data)
    htf_dirs = set(t['htf_dir'] for t in combined_data)

    combos = []

    # Try single filters first
    for regime in regimes:
        subset = [t for t in combined_data if t['regime'] == regime]
        if len(subset) >= 3:
            pnl = sum(t['pnl'] for t in subset)
            wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
            combos.append({
                'filter': f"regime={regime}",
                'trades': len(subset),
                'pnl': round(pnl, 2),
                'wr': round(wr, 1),
                'expectancy': round(pnl / len(subset), 2),
            })

    for session in sessions:
        subset = [t for t in combined_data if t['session'] == session]
        if len(subset) >= 3:
            pnl = sum(t['pnl'] for t in subset)
            wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
            combos.append({
                'filter': f"session={session}",
                'trades': len(subset),
                'pnl': round(pnl, 2),
                'wr': round(wr, 1),
                'expectancy': round(pnl / len(subset), 2),
            })

    for htf_dir in htf_dirs:
        subset = [t for t in combined_data if t['htf_dir'] == htf_dir]
        if len(subset) >= 3:
            pnl = sum(t['pnl'] for t in subset)
            wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
            combos.append({
                'filter': f"htf={htf_dir}",
                'trades': len(subset),
                'pnl': round(pnl, 2),
                'wr': round(wr, 1),
                'expectancy': round(pnl / len(subset), 2),
            })

    # Two-factor combos
    for regime in regimes:
        for session in sessions:
            subset = [t for t in combined_data if t['regime'] == regime and t['session'] == session]
            if len(subset) >= 3:
                pnl = sum(t['pnl'] for t in subset)
                wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
                combos.append({
                    'filter': f"regime={regime} + session={session}",
                    'trades': len(subset),
                    'pnl': round(pnl, 2),
                    'wr': round(wr, 1),
                    'expectancy': round(pnl / len(subset), 2),
                })

    for regime in regimes:
        for htf_dir in htf_dirs:
            subset = [t for t in combined_data if t['regime'] == regime and t['htf_dir'] == htf_dir]
            if len(subset) >= 3:
                pnl = sum(t['pnl'] for t in subset)
                wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
                combos.append({
                    'filter': f"regime={regime} + htf={htf_dir}",
                    'trades': len(subset),
                    'pnl': round(pnl, 2),
                    'wr': round(wr, 1),
                    'expectancy': round(pnl / len(subset), 2),
                })

    for session in sessions:
        for htf_dir in htf_dirs:
            subset = [t for t in combined_data if t['session'] == session and t['htf_dir'] == htf_dir]
            if len(subset) >= 3:
                pnl = sum(t['pnl'] for t in subset)
                wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
                combos.append({
                    'filter': f"session={session} + htf={htf_dir}",
                    'trades': len(subset),
                    'pnl': round(pnl, 2),
                    'wr': round(wr, 1),
                    'expectancy': round(pnl / len(subset), 2),
                })

    # Three-factor combos
    for regime in regimes:
        for session in sessions:
            for htf_dir in htf_dirs:
                subset = [t for t in combined_data
                          if t['regime'] == regime and t['session'] == session and t['htf_dir'] == htf_dir]
                if len(subset) >= 3:
                    pnl = sum(t['pnl'] for t in subset)
                    wr = sum(1 for t in subset if t['is_win']) / len(subset) * 100
                    combos.append({
                        'filter': f"regime={regime} + session={session} + htf={htf_dir}",
                        'trades': len(subset),
                        'pnl': round(pnl, 2),
                        'wr': round(wr, 1),
                        'expectancy': round(pnl / len(subset), 2),
                    })

    # Sort by expectancy descending
    combos.sort(key=lambda x: x['expectancy'], reverse=True)
    return combos


# ══════════════════════════════════════════════════════════════
#  PHASE 4 — Report Generation
# ══════════════════════════════════════════════════════════════

def generate_report(
    phase1_results: dict,
    phase2_results: dict,
    phase3_results: dict,
    combined_filters: list,
    output_path: str,
):
    lines = []
    lines.append("# MTF Confluence Analysis Report")
    lines.append(f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Data window**: Feb 1-26, 2026 (2m execution, all TF HTF data)")
    lines.append(f"**HC filter**: ON (score >= 0.75, stop <= 30pts, TP1 = 1.5x stop)")
    lines.append("")

    # ── PHASE 1 ──
    lines.append("---")
    lines.append("## Phase 1: HTF Confluence Value Test")
    lines.append("")
    lines.append("| Metric | Config A (No HTF) | Config B (gate=0.7) | Config C (gate=0.5) | Config D (gate=0.3) |")
    lines.append("|---|---|---|---|---|")

    configs = ['A', 'B', 'C', 'D']
    metrics = [
        ('Total Trades', 'total_trades', ''),
        ('HTF Blocked', 'htf_blocked', ''),
        ('Win Rate', 'win_rate', '%'),
        ('Profit Factor', 'profit_factor', ''),
        ('Total PnL', 'total_pnl', '$'),
        ('Avg Winner', 'avg_winner', '$'),
        ('Avg Loser', 'avg_loser', '$'),
        ('Largest Win', 'largest_win', '$'),
        ('Largest Loss', 'largest_loss', '$'),
        ('Max DD', 'max_drawdown_pct', '%'),
        ('C1 PnL', 'c1_pnl', '$'),
        ('C2 PnL', 'c2_pnl', '$'),
    ]

    for label, key, unit in metrics:
        row = f"| **{label}** |"
        for cfg in configs:
            val = phase1_results[cfg].get(key, 0)
            if unit == '$':
                row += f" ${val:,.2f} |"
            elif unit == '%':
                row += f" {val:.1f}% |"
            else:
                row += f" {val} |"
        lines.append(row)

    # Compute expectancy
    lines.append(f"| **Expectancy/trade** |")
    for cfg in configs:
        r = phase1_results[cfg]
        trades = r['total_trades']
        pnl = r['total_pnl']
        exp = pnl / trades if trades > 0 else 0
        lines[-1] = lines[-1].rstrip('|')  # remove trailing if we need to rebuild
    # Redo expectancy row properly
    lines.pop()
    row = "| **Expectancy/trade** |"
    for cfg in configs:
        r = phase1_results[cfg]
        trades = r['total_trades']
        pnl = r['total_pnl']
        exp = pnl / trades if trades > 0 else 0
        row += f" ${exp:,.2f} |"
    lines.append(row)

    lines.append("")

    # Identify best config
    best_cfg = max(configs, key=lambda c: phase1_results[c].get('profit_factor', 0))
    best_pf = phase1_results[best_cfg]['profit_factor']
    lines.append(f"**Best performing config**: {best_cfg} (PF {best_pf})")
    lines.append("")

    # ── PHASE 2 ──
    lines.append("---")
    lines.append("## Phase 2: Kill/Save Matrix")
    lines.append("")
    lines.append("Using Config A's trade universe, retroactively checking what each HTF gate would have done:")
    lines.append("")

    for gate_label, gate_val in [('B (0.7)', 0.7), ('C (0.5)', 0.5), ('D (0.3)', 0.3)]:
        m = phase2_results[gate_val]
        lines.append(f"### Config {gate_label}")
        lines.append("")
        lines.append("|  | HTF Allows | HTF Blocks |")
        lines.append("|---|---|---|")
        lines.append(f"| **Winner** | TP={m['tp']} (${m['tp_pnl']:,.2f}) | FN={m['fn']} (${m['fn_pnl']:,.2f}) |")
        lines.append(f"| **Loser** | FP={m['fp']} (${m['fp_pnl']:,.2f}) | TN={m['tn']} (${m['tn_pnl']:,.2f}) |")
        lines.append("")
        lines.append(f"- **Precision** (winners / allowed): {m['precision']:.1%}")
        lines.append(f"- **Recall** (winners kept / all winners): {m['recall']:.1%}")
        lines.append(f"- **F1 Score**: {m['f1']:.3f}")
        lines.append(f"- **PnL saved** (blocked losers): ${m['pnl_saved']:,.2f}")
        lines.append(f"- **PnL sacrificed** (blocked winners): ${m['fn_pnl']:,.2f}")
        lines.append(f"- **Net filter value**: ${m['net_filter_value']:,.2f}")
        lines.append("")

    # ── PHASE 3 ──
    lines.append("---")
    lines.append("## Phase 3: Regime + Session + HTF Cross-Analysis")
    lines.append("")
    lines.append(f"Analysis based on best-performing config ({best_cfg}).")
    lines.append("")

    # By Regime
    lines.append("### PnL by Regime")
    lines.append("")
    lines.append("| Regime | Trades | Wins | WR | PnL | Expectancy |")
    lines.append("|---|---|---|---|---|---|")
    for regime, data in sorted(phase3_results['by_regime'].items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = data['wins'] / data['trades'] * 100 if data['trades'] > 0 else 0
        exp = data['pnl'] / data['trades'] if data['trades'] > 0 else 0
        lines.append(f"| {regime} | {data['trades']} | {data['wins']} | {wr:.1f}% | ${data['pnl']:,.2f} | ${exp:,.2f} |")
    lines.append("")

    # By Session
    lines.append("### PnL by Session (ET)")
    lines.append("")
    lines.append("| Session | Trades | Wins | WR | PnL | Expectancy |")
    lines.append("|---|---|---|---|---|---|")
    for session, data in sorted(phase3_results['by_session'].items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = data['wins'] / data['trades'] * 100 if data['trades'] > 0 else 0
        exp = data['pnl'] / data['trades'] if data['trades'] > 0 else 0
        lines.append(f"| {session} | {data['trades']} | {data['wins']} | {wr:.1f}% | ${data['pnl']:,.2f} | ${exp:,.2f} |")
    lines.append("")

    # By HTF Direction
    lines.append("### PnL by HTF Consensus Direction at Entry")
    lines.append("")
    lines.append("| HTF Direction | Trades | Wins | WR | PnL | Expectancy |")
    lines.append("|---|---|---|---|---|---|")
    for htf_dir, data in sorted(phase3_results['by_htf_direction'].items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = data['wins'] / data['trades'] * 100 if data['trades'] > 0 else 0
        exp = data['pnl'] / data['trades'] if data['trades'] > 0 else 0
        lines.append(f"| {htf_dir} | {data['trades']} | {data['wins']} | {wr:.1f}% | ${data['pnl']:,.2f} | ${exp:,.2f} |")
    lines.append("")

    # Combined filters
    lines.append("### Combined Filter Search (min 3 trades)")
    lines.append("")
    lines.append("All filter combinations sorted by expectancy. **Positive expectancy** combos are highlighted.")
    lines.append("")
    lines.append("| Filter | Trades | WR | PnL | Expectancy/trade |")
    lines.append("|---|---|---|---|---|")

    positive_count = 0
    for combo in combined_filters:
        marker = "**" if combo['expectancy'] > 0 else ""
        lines.append(f"| {marker}{combo['filter']}{marker} | {combo['trades']} | {combo['wr']:.1f}% | ${combo['pnl']:,.2f} | ${combo['expectancy']:,.2f} |")
        if combo['expectancy'] > 0:
            positive_count += 1
    lines.append("")

    # ── RECOMMENDATION ──
    lines.append("---")
    lines.append("## Recommendation")
    lines.append("")

    # Check if any config is profitable
    profitable_configs = [c for c in configs if phase1_results[c]['total_pnl'] > 0]
    positive_combos = [c for c in combined_filters if c['expectancy'] > 0 and c['trades'] >= 5]

    if profitable_configs:
        lines.append(f"### HTF engine makes the system profitable")
        for cfg in profitable_configs:
            r = phase1_results[cfg]
            lines.append(f"- Config {cfg}: {r['total_trades']} trades, PF {r['profit_factor']}, ${r['total_pnl']:,.2f}")
        lines.append("")
        lines.append(f"**Adopt Config {profitable_configs[0]}** as the production HTF configuration.")
    elif positive_combos:
        lines.append(f"### No single HTF config flips February positive, but combined filters show edge")
        lines.append("")
        lines.append("Top positive-expectancy combined filters (min 5 trades):")
        for combo in positive_combos[:5]:
            lines.append(f"- **{combo['filter']}**: {combo['trades']} trades, {combo['wr']:.0f}% WR, ${combo['pnl']:,.2f} PnL, ${combo['expectancy']:,.2f}/trade")
        lines.append("")
        lines.append("**Recommendation**: Implement these as additional soft gates on top of the HC filter.")
    else:
        lines.append("### February 2026 is a hostile regime for this strategy")
        lines.append("")
        lines.append("No HTF configuration or combined filter produces consistent positive edge on this data window.")
        lines.append("")
        # Still give the least-bad option
        least_bad = min(configs, key=lambda c: abs(phase1_results[c]['total_pnl']))
        r = phase1_results[least_bad]
        lines.append(f"**Least-bad config**: {least_bad} ({r['total_trades']} trades, PF {r['profit_factor']}, ${r['total_pnl']:,.2f})")
        lines.append("")
        lines.append("**Recommendation**: The HTF engine needs redesign. Current implementation (simple close-vs-open trend) lacks the resolution to filter effectively in a choppy/hostile regime. Consider:")
        lines.append("- Weighted voting (higher TFs get more weight)")
        lines.append("- Momentum-based bias (RSI/MACD on HTF) instead of close-vs-open")
        lines.append("- Volatility regime awareness in the HTF engine itself")
        lines.append("- Session-aware gating (different HTF rules for different times of day)")

    lines.append("")
    lines.append("---")
    lines.append(f"*Analysis completed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    report = "\n".join(lines)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(report)

    return report


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  MTF CONFLUENCE ANALYSIS")
    print("=" * 60)

    # ── PHASE 1 ──
    print("\n[PHASE 1] Running 4 configurations...")

    print("  Config A: Single-TF, no HTF engine...")
    summary_a, trades_a = await run_config_a()
    print(f"    -> {summary_a['total_trades']} trades, PF {summary_a['profit_factor']}, ${summary_a['total_pnl']:,.2f}")

    print("  Config B: HTF gate = 0.7 (default)...")
    summary_b, trades_b = await run_config_mtf(0.7, "B")
    print(f"    -> {summary_b['total_trades']} trades, PF {summary_b['profit_factor']}, ${summary_b['total_pnl']:,.2f}")

    print("  Config C: HTF gate = 0.5 (aggressive)...")
    summary_c, trades_c = await run_config_mtf(0.5, "C")
    print(f"    -> {summary_c['total_trades']} trades, PF {summary_c['profit_factor']}, ${summary_c['total_pnl']:,.2f}")

    print("  Config D: HTF gate = 0.3 (maximum)...")
    summary_d, trades_d = await run_config_mtf(0.3, "D")
    print(f"    -> {summary_d['total_trades']} trades, PF {summary_d['profit_factor']}, ${summary_d['total_pnl']:,.2f}")

    phase1_results = {
        'A': summary_a,
        'B': summary_b,
        'C': summary_c,
        'D': summary_d,
    }

    # ── PHASE 2 ──
    print("\n[PHASE 2] Building HTF replay engine for kill/save analysis...")
    htf_snapshots = await build_htf_replay_engine()
    print(f"    -> {len(htf_snapshots)} HTF snapshots recorded")

    # Use Config A's CLOSED trades for kill/save analysis
    closed_a = [t for t in trades_a if t.get('action') == 'trade_closed']
    # We need entry timestamps too — pair them up
    entries_a = [t for t in trades_a if t.get('action') == 'entry']

    # Attach entry timestamp to closed trades
    entry_idx = 0
    for trade in closed_a:
        # Find the matching entry
        while entry_idx < len(entries_a):
            entry = entries_a[entry_idx]
            if entry.get('_bar_timestamp'):
                trade['_bar_timestamp'] = entry['_bar_timestamp']
                entry_idx += 1
                break
            entry_idx += 1

    phase2_results = {}
    for gate in [0.7, 0.5, 0.3]:
        m = compute_kill_save_matrix(closed_a, htf_snapshots, gate)
        phase2_results[gate] = m
        print(f"    Gate {gate}: TP={m['tp']} TN={m['tn']} FP={m['fp']} FN={m['fn']} | "
              f"Precision={m['precision']:.1%} Recall={m['recall']:.1%} | "
              f"Net value=${m['net_filter_value']:,.2f}")

    # ── PHASE 3 ──
    print("\n[PHASE 3] Cross-analysis on best-performing config...")

    # Determine best config
    best_cfg = max(['A', 'B', 'C', 'D'], key=lambda c: phase1_results[c].get('profit_factor', 0))
    best_trades_map = {'A': trades_a, 'B': trades_b, 'C': trades_c, 'D': trades_d}
    best_trades = best_trades_map[best_cfg]

    print(f"    Best config: {best_cfg}")

    phase3_results = analyze_cross_dimensions(best_trades, htf_snapshots)
    combined_filters = find_combined_filters(phase3_results['combined_filters'])

    positive = [c for c in combined_filters if c['expectancy'] > 0]
    print(f"    {len(combined_filters)} filter combos tested, {len(positive)} have positive expectancy")

    if positive:
        print(f"    Top combo: {positive[0]['filter']} -> {positive[0]['trades']} trades, ${positive[0]['expectancy']:,.2f}/trade")

    # ── PHASE 4 ──
    print("\n[PHASE 4] Generating report...")

    output_path = os.path.join(
        os.path.dirname(__file__), '..', 'docs', 'mtf_confluence_analysis.md'
    )

    report = generate_report(
        phase1_results, phase2_results, phase3_results, combined_filters, output_path
    )

    print(f"    Report saved to: {output_path}")
    print(f"    Report length: {len(report)} chars, {report.count(chr(10))} lines")

    # Also dump raw data as JSON for potential further analysis
    raw_data_path = output_path.replace('.md', '_raw.json')
    raw = {
        'phase1': phase1_results,
        'phase2': {str(k): v for k, v in phase2_results.items()},
        'phase3_regime': {k: dict(v) for k, v in phase3_results['by_regime'].items()},
        'phase3_session': {k: dict(v) for k, v in phase3_results['by_session'].items()},
        'phase3_htf': {k: dict(v) for k, v in phase3_results['by_htf_direction'].items()},
        'combined_filters': combined_filters[:20],  # Top 20
    }
    with open(raw_data_path, 'w') as f:
        json.dump(raw, f, indent=2, default=str)
    print(f"    Raw data saved to: {raw_data_path}")

    print("\n" + "=" * 60)
    print("  ANALYSIS COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
