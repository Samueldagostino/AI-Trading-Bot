#!/usr/bin/env python3
"""
Runner script for Session Handoff Analysis.

Loads historical MNQ 1-min CSV data, runs the SessionHandoffAnalyzer,
and outputs results to both console and docs/session_handoff_probabilities.md.

RESEARCH / OBSERVATION ONLY — not a trading strategy.
"""

import os
import sys
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nq_bot_vscode.research.session_handoff_analyzer import (
    HANDOFF_PAIRS,
    HandoffOutcome,
    SessionBehavior,
    SessionHandoffAnalyzer,
    SessionName,
)


def find_data_file() -> str:
    """Locate best available historical CSV file."""
    candidates = [
        PROJECT_ROOT / "nq_bot_vscode" / "data" / "historical" / "combined_1min.csv",
        PROJECT_ROOT / "data" / "firstrate" / "historical" / "NQ_1m_2022-09_to_2023-08_merged.csv",
        PROJECT_ROOT / "nq_bot_vscode" / "data" / "firstrate" / "NQ_1m.csv",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError(
        "No historical CSV data found. Checked:\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


def generate_markdown_report(analyzer: SessionHandoffAnalyzer) -> str:
    """Generate full Markdown report."""
    lines = []
    lines.append("# Session Handoff Conditional Probability Analysis")
    lines.append("")
    lines.append("> **OBSERVATION ONLY** — Not a trading strategy. No trading decisions")
    lines.append("> should be based on this until we have 6+ months of observations AND")
    lines.append("> statistically significant, reproducible results.")
    lines.append("")

    # Data coverage
    coverage = analyzer.get_data_coverage()
    lines.append("## Data Coverage")
    lines.append("")
    lines.append(f"- **Period**: {coverage['start']} to {coverage['end']}")
    lines.append(f"- **Duration**: {coverage['months']} months")
    lines.append(f"- **Trading days**: {coverage['trading_days']}")
    lines.append(f"- **Total sessions**: {coverage['total_sessions']}")
    lines.append(f"- **Status**: {coverage['label']}")
    lines.append("")

    # Methodology
    lines.append("## Methodology")
    lines.append("")
    lines.append("### Session Definitions (all times ET)")
    lines.append("| Session | Start | End |")
    lines.append("|---------|-------|-----|")
    lines.append("| Asia | 18:00 | 02:00 |")
    lines.append("| London | 02:00 | 08:00 |")
    lines.append("| NY Open | 08:00 | 10:30 |")
    lines.append("| NY Core | 10:30 | 15:00 |")
    lines.append("| NY Close | 15:00 | 16:00 |")
    lines.append("")

    lines.append("### Session Behavior Classification")
    lines.append("| Behavior | Criteria |")
    lines.append("|----------|----------|")
    lines.append("| STRONG_TREND_UP | Return > +0.3%, close in top 20% of range |")
    lines.append("| STRONG_TREND_DOWN | Return < -0.3%, close in bottom 20% of range |")
    lines.append("| WEAK_TREND_UP | Return +0.1% to +0.3% |")
    lines.append("| WEAK_TREND_DOWN | Return -0.3% to -0.1% |")
    lines.append("| RANGE_BOUND | Return -0.1% to +0.1%, range < median |")
    lines.append("| SPIKE_REVERSAL | Range > 0.4%, close near open (< 0.1% net) |")
    lines.append("| EXPANSION | Range > 1.5x median (high volatility) |")
    lines.append("")

    lines.append("### Handoff Outcome Classification")
    lines.append("| Outcome | Criteria |")
    lines.append("|---------|----------|")
    lines.append("| CONTINUATION | Next session moves same direction |")
    lines.append("| REVERSAL | Next session moves opposite > 0.15% |")
    lines.append("| RANGE | Next session stays within 0.1% of prev close |")
    lines.append("")

    any_significant = False

    # Handoff matrices
    for from_s, to_s in HANDOFF_PAIRS:
        lines.append(f"## {from_s.value} → {to_s.value}")
        lines.append("")

        matrix = analyzer.get_handoff_matrix(from_s, to_s)
        chi_results = analyzer.chi_squared_test(from_s, to_s)

        # Table header
        lines.append(
            "| Behavior | CONTINUATION | REVERSAL | RANGE | N | p-value | Significant? |"
        )
        lines.append("|----------|-------------|----------|-------|---|---------|-------------|")

        for behavior in SessionBehavior:
            row = matrix[behavior]
            chi = chi_results[behavior]
            n = row[HandoffOutcome.CONTINUATION].total

            p_cont = row[HandoffOutcome.CONTINUATION].probability
            p_rev = row[HandoffOutcome.REVERSAL].probability
            p_rng = row[HandoffOutcome.RANGE].probability

            p_val = chi.get("p_value")
            sig = chi.get("significant", False)
            if sig:
                any_significant = True

            reliability = ""
            if n < 20:
                reliability = " **UNRELIABLE**"
            elif n < 30:
                reliability = " *low-N*"

            p_str = f"{p_val:.4f}" if p_val is not None else "N/A"
            sig_str = "**YES**" if sig else "no"

            lines.append(
                f"| {behavior.value} | {p_cont:.1%} | {p_rev:.1%} | {p_rng:.1%} | "
                f"{n}{reliability} | {p_str} | {sig_str} |"
            )

        lines.append("")

        # CIs for reliable cells
        lines.append("<details><summary>95% Confidence Intervals</summary>")
        lines.append("")
        lines.append("| Behavior | CONT CI | REV CI | RANGE CI |")
        lines.append("|----------|---------|--------|----------|")
        for behavior in SessionBehavior:
            row = matrix[behavior]
            n = row[HandoffOutcome.CONTINUATION].total
            if n < 20:
                continue
            ci_c = row[HandoffOutcome.CONTINUATION].ci_95
            ci_r = row[HandoffOutcome.REVERSAL].ci_95
            ci_g = row[HandoffOutcome.RANGE].ci_95
            lines.append(
                f"| {behavior.value} | [{ci_c[0]:.1%}, {ci_c[1]:.1%}] | "
                f"[{ci_r[0]:.1%}, {ci_r[1]:.1%}] | [{ci_g[0]:.1%}, {ci_g[1]:.1%}] |"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

        # Survivorship bias
        surv = analyzer.survivorship_bias_test(from_s, to_s)
        if "error" not in surv:
            lines.append("### Survivorship Bias Test")
            lines.append(f"- Split date: {surv['split_date']}")
            h1 = surv["first_half"]
            h2 = surv["second_half"]
            p1 = f"p={h1['p_value']:.4f}" if h1.get("p_value") is not None else "N/A"
            p2 = f"p={h2['p_value']:.4f}" if h2.get("p_value") is not None else "N/A"
            lines.append(f"- First half: N={h1['n']}, {p1}")
            lines.append(f"- Second half: N={h2['n']}, {p2}")
            lines.append(
                f"- **Consistent edge**: {'YES' if surv['consistent_edge'] else 'NO'}"
            )
            lines.append("")

        # Regime test
        regime = analyzer.regime_test(from_s, to_s)
        if "error" not in regime:
            lines.append("### Regime Test (High-Vol vs Low-Vol)")
            hv = regime["high_vol"]
            lv = regime["low_vol"]
            p_hv = f"p={hv['p_value']:.4f}" if hv.get("p_value") is not None else "N/A"
            p_lv = f"p={lv['p_value']:.4f}" if lv.get("p_value") is not None else "N/A"
            lines.append(f"- High-vol: N={hv['n']}, {p_hv}")
            lines.append(f"- Low-vol: N={lv['n']}, {p_lv}")
            lines.append(
                f"- **Consistent edge**: {'YES' if regime['consistent_edge'] else 'NO'}"
            )
            lines.append("")

    # Selection bias test
    lines.append("## Selection Bias Test")
    lines.append("")
    lines.append("Compares volatility in first 30 minutes of session opens vs random 30-minute windows.")
    lines.append("")
    sb = analyzer.run_selection_bias_test()
    if "error" not in sb:
        lines.append(f"- Session open mean range: {sb['session_open_mean_range']:.5f}")
        lines.append(f"- Random window mean range: {sb['random_mean_range']:.5f}")
        lines.append(f"- Ratio: {sb['ratio']:.2f}x")
        lines.append(f"- Mann-Whitney U p-value: {sb['p_value']:.6f}")
        lines.append(f"- **Verdict**: {sb['verdict']}")
    else:
        lines.append(f"- Error: {sb.get('error', 'unknown')}")
    lines.append("")

    # Transaction cost analysis
    lines.append("## Transaction Cost Analysis")
    lines.append("")
    lines.append("- Round-trip cost: $1.24 per contract (MNQ)")
    lines.append("- MNQ point value: $2.00")
    lines.append("- Minimum edge needed: 0.62 points (~0.003% at NQ 20000)")
    lines.append("")

    # Final verdict
    lines.append("## Verdict")
    lines.append("")
    if any_significant:
        lines.append("**STATISTICALLY SIGNIFICANT CELLS FOUND (p < 0.05)**")
        lines.append("")
        lines.append("However, statistical significance does NOT equal a trading edge.")
        lines.append("Before acting on any finding:")
        lines.append("1. Check effect sizes (Cramér's V) — are deviations large enough to trade?")
        lines.append("2. Check survivorship test — does the edge persist in both halves?")
        lines.append("3. Check regime test — does the edge persist in both vol regimes?")
        lines.append("4. After $1.24 round-trip costs, is there still positive expectancy?")
        lines.append("5. Collect 6+ months of live observation before any implementation.")
    else:
        lines.append("**NO EDGE FOUND — session handoffs appear random**")
        lines.append("")
        lines.append("No cell showed p < 0.05 against uniform (33/33/33) distribution.")
        lines.append("Session handoff behavior does not deviate significantly from chance.")
    lines.append("")

    lines.append("---")
    lines.append("*Generated by SessionHandoffAnalyzer — research/observation only.*")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("Session Handoff Analysis — RESEARCH ONLY")
    print("=" * 60)
    print()

    # Find data
    try:
        data_path = find_data_file()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)

    print(f"Loading data from: {data_path}")
    print("This may take a moment for large files...")
    print()

    # Run analysis
    analyzer = SessionHandoffAnalyzer()
    analyzer.analyze_csv(data_path)

    # Print console report
    report = analyzer.print_full_report()
    print(report)

    # Generate and save markdown report
    docs_dir = PROJECT_ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    md_path = docs_dir / "session_handoff_probabilities.md"

    md_report = generate_markdown_report(analyzer)
    md_path.write_text(md_report)
    print(f"\nMarkdown report saved to: {md_path}")

    # Print Asia → London matrix specifically (as requested)
    print("\n" + "=" * 60)
    print("ASIA → LONDON HANDOFF MATRIX (requested output)")
    print("=" * 60)
    matrix = analyzer.get_handoff_matrix(SessionName.ASIA, SessionName.LONDON)
    chi = analyzer.chi_squared_test(SessionName.ASIA, SessionName.LONDON)

    any_sig = False
    for behavior in SessionBehavior:
        row = matrix[behavior]
        chi_row = chi[behavior]
        n = row[HandoffOutcome.CONTINUATION].total
        sig = chi_row.get("significant", False)
        if sig:
            any_sig = True
        print(
            f"  {behavior.value:<22} "
            f"CONT={row[HandoffOutcome.CONTINUATION].probability:.1%}  "
            f"REV={row[HandoffOutcome.REVERSAL].probability:.1%}  "
            f"RANGE={row[HandoffOutcome.RANGE].probability:.1%}  "
            f"N={n}"
            f"{'  *** SIGNIFICANT ***' if sig else ''}"
        )

    print()
    if any_sig:
        print("EDGE FOUND in Asia → London: see significant cells above")
        print("WARNING: Verify with survivorship bias test and effect sizes.")
    else:
        print("NO EDGE FOUND — Asia → London session handoffs are random")

    # Print sample sizes
    print("\nSample sizes per cell:")
    sizes = analyzer.get_sample_sizes()
    for (from_s, to_s), counts in sizes.items():
        total = sum(counts.values())
        meaningful = sum(1 for c in counts.values() if c >= 50)
        unreliable = sum(1 for c in counts.values() if c < 20)
        print(
            f"  {from_s.value:>10} → {to_s.value:<10}: "
            f"total={total}, meaningful(N≥50)={meaningful}, unreliable(N<20)={unreliable}"
        )


if __name__ == "__main__":
    main()
