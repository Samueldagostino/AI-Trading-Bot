"""
Session Handoff Conditional Probability Analyzer

OBSERVATION TOOL ONLY — not a trading strategy.
Calculates conditional probabilities of continuation, reversal, and range-bound
behavior across Asia → London → NY sessions using historical MNQ 1-minute data.

No trading decisions should be based on this until we have 6+ months of observations
AND statistically significant results.
"""

import warnings
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SessionName(Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NY_OPEN = "NY_OPEN"
    NY_CORE = "NY_CORE"
    NY_CLOSE = "NY_CLOSE"


class SessionBehavior(Enum):
    STRONG_TREND_UP = "STRONG_TREND_UP"
    STRONG_TREND_DOWN = "STRONG_TREND_DOWN"
    WEAK_TREND_UP = "WEAK_TREND_UP"
    WEAK_TREND_DOWN = "WEAK_TREND_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    SPIKE_REVERSAL = "SPIKE_REVERSAL"
    EXPANSION = "EXPANSION"


class HandoffOutcome(Enum):
    CONTINUATION = "CONTINUATION"
    REVERSAL = "REVERSAL"
    RANGE = "RANGE"


# ---------------------------------------------------------------------------
# Session time definitions (ET / US-Eastern)
# ---------------------------------------------------------------------------

SESSION_TIMES = {
    SessionName.ASIA:     (time(18, 0), time(2, 0)),    # 18:00–02:00 ET (crosses midnight)
    SessionName.LONDON:   (time(2, 0),  time(8, 0)),    # 02:00–08:00 ET
    SessionName.NY_OPEN:  (time(8, 0),  time(10, 30)),  # 08:00–10:30 ET
    SessionName.NY_CORE:  (time(10, 30), time(15, 0)),  # 10:30–15:00 ET
    SessionName.NY_CLOSE: (time(15, 0), time(16, 0)),   # 15:00–16:00 ET
}

# Consecutive session pairs for handoff analysis
HANDOFF_PAIRS = [
    (SessionName.ASIA, SessionName.LONDON),
    (SessionName.LONDON, SessionName.NY_OPEN),
    (SessionName.NY_OPEN, SessionName.NY_CORE),
    (SessionName.NY_CORE, SessionName.NY_CLOSE),
    # Skip-session pairs
    (SessionName.ASIA, SessionName.NY_OPEN),
]


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    """Aggregated OHLCV for one session instance."""
    session_name: SessionName
    date: datetime  # trading date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    bar_count: int

    @property
    def session_return(self) -> float:
        if self.open_price == 0:
            return 0.0
        return (self.close_price - self.open_price) / self.open_price

    @property
    def session_range(self) -> float:
        if self.open_price == 0:
            return 0.0
        return (self.high_price - self.low_price) / self.open_price

    @property
    def close_position_in_range(self) -> float:
        """0.0 = closed at low, 1.0 = closed at high."""
        rng = self.high_price - self.low_price
        if rng == 0:
            return 0.5
        return (self.close_price - self.low_price) / rng


@dataclass
class HandoffObservation:
    date: datetime
    from_session: SessionName
    to_session: SessionName
    from_behavior: SessionBehavior
    to_behavior: SessionBehavior
    from_return: float
    to_return: float
    outcome: HandoffOutcome


@dataclass
class ProbabilityCell:
    count: int = 0
    total: int = 0

    @property
    def probability(self) -> float:
        return self.count / self.total if self.total > 0 else 0.0

    @property
    def ci_95(self) -> tuple[float, float]:
        """Wilson score interval for binomial proportion."""
        if self.total == 0:
            return (0.0, 0.0)
        return _wilson_ci(self.count, self.total, 0.95)

    @property
    def is_reliable(self) -> bool:
        return self.total >= 30

    @property
    def is_meaningful(self) -> bool:
        return self.total >= 50


def _wilson_ci(successes: int, total: int, confidence: float) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if total == 0:
        return (0.0, 0.0)
    z = stats.norm.ppf(1 - (1 - confidence) / 2)
    p_hat = successes / total
    denom = 1 + z ** 2 / total
    center = (p_hat + z ** 2 / (2 * total)) / denom
    spread = z * np.sqrt((p_hat * (1 - p_hat) + z ** 2 / (4 * total)) / total) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


# ---------------------------------------------------------------------------
# Main Analyzer
# ---------------------------------------------------------------------------

class SessionHandoffAnalyzer:
    """
    Calculates conditional probabilities of session handoff outcomes.
    RESEARCH / OBSERVATION ONLY.
    """

    def __init__(self):
        self.sessions: list[SessionStats] = []
        self.observations: list[HandoffObservation] = []
        self.median_ranges: dict[SessionName, float] = {}
        self._data_start: Optional[datetime] = None
        self._data_end: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_csv(self, filepath: str) -> None:
        """Load and process historical 1-min CSV data."""
        df = self._load_csv(filepath)
        self._extract_sessions(df)
        self._compute_median_ranges()
        self._classify_all_sessions()
        self._build_handoff_observations()

    def get_handoff_matrix(
        self, from_session: SessionName, to_session: SessionName
    ) -> dict[SessionBehavior, dict[HandoffOutcome, ProbabilityCell]]:
        """Return probability table for a specific session pair."""
        relevant = [
            o for o in self.observations
            if o.from_session == from_session and o.to_session == to_session
        ]
        matrix: dict[SessionBehavior, dict[HandoffOutcome, ProbabilityCell]] = {}
        for behavior in SessionBehavior:
            row_obs = [o for o in relevant if o.from_behavior == behavior]
            total = len(row_obs)
            row: dict[HandoffOutcome, ProbabilityCell] = {}
            for outcome in HandoffOutcome:
                count = sum(1 for o in row_obs if o.outcome == outcome)
                row[outcome] = ProbabilityCell(count=count, total=total)
            matrix[behavior] = row
        return matrix

    def get_sample_sizes(self) -> dict[tuple[SessionName, SessionName], dict[SessionBehavior, int]]:
        """Return observation counts per cell."""
        result = {}
        for from_s, to_s in HANDOFF_PAIRS:
            relevant = [
                o for o in self.observations
                if o.from_session == from_s and o.to_session == to_s
            ]
            counts = {}
            for behavior in SessionBehavior:
                counts[behavior] = sum(1 for o in relevant if o.from_behavior == behavior)
            result[(from_s, to_s)] = counts
        return result

    def get_confidence_intervals(
        self, from_session: SessionName, to_session: SessionName
    ) -> dict[SessionBehavior, dict[HandoffOutcome, tuple[float, float]]]:
        """Return 95% CIs for each probability cell."""
        matrix = self.get_handoff_matrix(from_session, to_session)
        result = {}
        for behavior, row in matrix.items():
            result[behavior] = {outcome: cell.ci_95 for outcome, cell in row.items()}
        return result

    def chi_squared_test(
        self, from_session: SessionName, to_session: SessionName
    ) -> dict[SessionBehavior, dict]:
        """
        Chi-squared goodness-of-fit test against uniform (33/33/33) distribution
        for each behavior row.
        """
        matrix = self.get_handoff_matrix(from_session, to_session)
        results = {}
        for behavior, row in matrix.items():
            total = row[HandoffOutcome.CONTINUATION].total
            if total < 5:
                results[behavior] = {
                    "chi2": None, "p_value": None, "significant": False,
                    "message": f"N={total} too small for chi-squared test",
                }
                continue
            observed = np.array([row[o].count for o in HandoffOutcome])
            expected = np.array([total / 3.0] * 3)
            chi2, p_value = stats.chisquare(observed, expected)
            # Effect size: Cramér's V (for 1×3, df=2)
            cramers_v = np.sqrt(chi2 / (total * 2)) if total > 0 else 0.0
            results[behavior] = {
                "chi2": float(chi2),
                "p_value": float(p_value),
                "significant": p_value < 0.05,
                "cramers_v": float(cramers_v),
                "observed": observed.tolist(),
                "expected": expected.tolist(),
                "n": total,
            }
        return results

    def survivorship_bias_test(
        self, from_session: SessionName, to_session: SessionName
    ) -> dict:
        """
        Split data 50/50 by date, run chi-squared on each half independently.
        Edge must appear in both halves to be considered real.
        """
        relevant = [
            o for o in self.observations
            if o.from_session == from_session and o.to_session == to_session
        ]
        if len(relevant) < 20:
            return {"error": f"Only {len(relevant)} observations — cannot split"}

        dates = sorted(set(o.date for o in relevant))
        midpoint = dates[len(dates) // 2]
        first_half = [o for o in relevant if o.date <= midpoint]
        second_half = [o for o in relevant if o.date > midpoint]

        def _aggregate_chi2(obs_list):
            total = len(obs_list)
            if total < 5:
                return {"chi2": None, "p_value": None, "n": total}
            counts = [sum(1 for o in obs_list if o.outcome == oc) for oc in HandoffOutcome]
            observed = np.array(counts)
            expected = np.array([total / 3.0] * 3)
            chi2, p_value = stats.chisquare(observed, expected)
            return {
                "chi2": float(chi2), "p_value": float(p_value), "n": total,
                "observed": observed.tolist(),
            }

        h1 = _aggregate_chi2(first_half)
        h2 = _aggregate_chi2(second_half)
        consistent = (
            h1.get("p_value") is not None
            and h2.get("p_value") is not None
            and h1["p_value"] < 0.05
            and h2["p_value"] < 0.05
        )
        return {
            "first_half": h1,
            "second_half": h2,
            "split_date": str(midpoint),
            "consistent_edge": consistent,
        }

    def regime_test(
        self, from_session: SessionName, to_session: SessionName
    ) -> dict:
        """
        Check if edge exists in both high-vol and low-vol periods.
        Split by median daily range.
        """
        relevant = [
            o for o in self.observations
            if o.from_session == from_session and o.to_session == to_session
        ]
        if len(relevant) < 20:
            return {"error": f"Only {len(relevant)} observations — cannot split"}

        # Get session ranges for each observation date
        date_ranges = {}
        for s in self.sessions:
            if s.date not in date_ranges:
                date_ranges[s.date] = 0.0
            date_ranges[s.date] += s.session_range

        median_daily_range = np.median(list(date_ranges.values())) if date_ranges else 0.0

        high_vol = [o for o in relevant if date_ranges.get(o.date, 0) >= median_daily_range]
        low_vol = [o for o in relevant if date_ranges.get(o.date, 0) < median_daily_range]

        def _aggregate(obs_list):
            total = len(obs_list)
            if total < 5:
                return {"chi2": None, "p_value": None, "n": total}
            counts = [sum(1 for o in obs_list if o.outcome == oc) for oc in HandoffOutcome]
            observed = np.array(counts)
            expected = np.array([total / 3.0] * 3)
            chi2, p_value = stats.chisquare(observed, expected)
            return {"chi2": float(chi2), "p_value": float(p_value), "n": total,
                    "observed": observed.tolist()}

        hv = _aggregate(high_vol)
        lv = _aggregate(low_vol)
        consistent = (
            hv.get("p_value") is not None
            and lv.get("p_value") is not None
            and hv["p_value"] < 0.05
            and lv["p_value"] < 0.05
        )
        return {
            "high_vol": hv, "low_vol": lv,
            "median_daily_range": float(median_daily_range),
            "consistent_edge": consistent,
        }

    def selection_bias_test(self, df: Optional[pd.DataFrame] = None) -> dict:
        """
        Compare range in first 30 minutes of each session open vs random
        30-minute windows. Tests whether session opens are actually special.
        """
        if df is None:
            return {"error": "Must provide DataFrame for selection bias test"}

        # Measure range at session opens (first 30 bars of each session)
        session_open_ranges = []
        session_starts = [time(18, 0), time(2, 0), time(8, 0), time(10, 30), time(15, 0)]

        for t in session_starts:
            mask = df["time_only"] == t if "time_only" in df.columns else pd.Series(False, index=df.index)
            if mask.sum() == 0:
                continue
            for idx in df.index[mask]:
                pos = df.index.get_loc(idx)
                window = df.iloc[pos:pos + 30]
                if len(window) >= 20:
                    rng = (window["high"].max() - window["low"].min())
                    open_price = window["open"].iloc[0]
                    if open_price > 0:
                        session_open_ranges.append(rng / open_price)

        # Random 30-minute windows (sample 200)
        rng_gen = np.random.RandomState(42)
        random_ranges = []
        n_attempts = 0
        while len(random_ranges) < 200 and n_attempts < 1000:
            idx = rng_gen.randint(0, max(1, len(df) - 30))
            window = df.iloc[idx:idx + 30]
            if len(window) >= 20:
                rng_val = (window["high"].max() - window["low"].min())
                open_price = window["open"].iloc[0]
                if open_price > 0:
                    random_ranges.append(rng_val / open_price)
            n_attempts += 1

        if len(session_open_ranges) < 10 or len(random_ranges) < 10:
            return {
                "error": "Not enough data for selection bias test",
                "session_open_n": len(session_open_ranges),
                "random_n": len(random_ranges),
            }

        # Mann-Whitney U test (non-parametric, doesn't assume normality)
        u_stat, p_value = stats.mannwhitneyu(
            session_open_ranges, random_ranges, alternative="greater"
        )
        session_mean = float(np.mean(session_open_ranges))
        random_mean = float(np.mean(random_ranges))

        return {
            "session_open_mean_range": session_mean,
            "random_mean_range": random_mean,
            "ratio": session_mean / random_mean if random_mean > 0 else float("inf"),
            "u_statistic": float(u_stat),
            "p_value": float(p_value),
            "session_opens_special": p_value < 0.05,
            "session_open_n": len(session_open_ranges),
            "random_n": len(random_ranges),
            "verdict": (
                "SESSION OPENS HAVE SIGNIFICANTLY HIGHER VOLATILITY"
                if p_value < 0.05
                else "SESSION OPEN VOLATILITY IS NOT SPECIAL — confirmation bias"
            ),
        }

    def get_data_coverage(self) -> dict:
        """Return data coverage information."""
        if not self.sessions:
            return {
                "start": "N/A", "end": "N/A", "months": 0,
                "trading_days": 0, "total_sessions": 0, "label": "NO DATA",
            }
        dates = [s.date for s in self.sessions]
        start = min(dates)
        end = max(dates)
        months = (end.year - start.year) * 12 + (end.month - start.month)
        if months < 3:
            label = "INSUFFICIENT DATA — results unreliable"
        elif months < 6:
            label = "PRELIMINARY — needs more data"
        else:
            label = "USABLE — but verify with live observation"
        return {
            "start": str(start.date()) if hasattr(start, 'date') else str(start),
            "end": str(end.date()) if hasattr(end, 'date') else str(end),
            "months": months,
            "trading_days": len(set(s.date for s in self.sessions)),
            "total_sessions": len(self.sessions),
            "label": label,
        }

    def print_full_report(self) -> str:
        """Generate formatted report of all matrices."""
        lines = []
        lines.append("=" * 80)
        lines.append("SESSION HANDOFF CONDITIONAL PROBABILITY ANALYSIS")
        lines.append("OBSERVATION ONLY — NOT A TRADING STRATEGY")
        lines.append("=" * 80)
        lines.append("")

        coverage = self.get_data_coverage()
        lines.append(f"Data: {coverage['start']} to {coverage['end']}")
        lines.append(f"Coverage: {coverage['months']} months ({coverage['trading_days']} trading days)")
        lines.append(f"Status: {coverage['label']}")
        lines.append(f"Total session observations: {coverage['total_sessions']}")
        lines.append("")

        any_significant = False

        for from_s, to_s in HANDOFF_PAIRS:
            lines.append("-" * 80)
            lines.append(f"HANDOFF: {from_s.value} → {to_s.value}")
            lines.append("-" * 80)

            matrix = self.get_handoff_matrix(from_s, to_s)
            chi_results = self.chi_squared_test(from_s, to_s)

            # Header
            lines.append(
                f"{'Behavior':<22} {'CONT':>8} {'REV':>8} {'RANGE':>8} "
                f"{'N':>5} {'p-val':>8} {'Sig?':>5}"
            )
            lines.append("-" * 70)

            for behavior in SessionBehavior:
                row = matrix[behavior]
                chi = chi_results[behavior]
                n = row[HandoffOutcome.CONTINUATION].total

                p_cont = row[HandoffOutcome.CONTINUATION].probability
                p_rev = row[HandoffOutcome.REVERSAL].probability
                p_rng = row[HandoffOutcome.RANGE].probability

                ci_cont = row[HandoffOutcome.CONTINUATION].ci_95
                ci_rev = row[HandoffOutcome.REVERSAL].ci_95
                ci_rng = row[HandoffOutcome.RANGE].ci_95

                p_val = chi.get("p_value")
                sig = chi.get("significant", False)
                if sig:
                    any_significant = True

                reliability = ""
                if n < 20:
                    reliability = " UNRELIABLE"
                elif n < 30:
                    reliability = " LOW-N"

                p_str = f"{p_val:.4f}" if p_val is not None else "  N/A "
                sig_str = " YES" if sig else "  no"

                lines.append(
                    f"{behavior.value:<22} {p_cont:>7.1%} {p_rev:>7.1%} {p_rng:>7.1%} "
                    f"{n:>5} {p_str:>8} {sig_str:>5}{reliability}"
                )
                if n >= 20:
                    lines.append(
                        f"  95% CI:              "
                        f"[{ci_cont[0]:.1%},{ci_cont[1]:.1%}] "
                        f"[{ci_rev[0]:.1%},{ci_rev[1]:.1%}] "
                        f"[{ci_rng[0]:.1%},{ci_rng[1]:.1%}]"
                    )

            lines.append("")

            # Survivorship test
            surv = self.survivorship_bias_test(from_s, to_s)
            if "error" not in surv:
                lines.append(f"  Survivorship bias test (split at {surv['split_date']}):")
                h1 = surv["first_half"]
                h2 = surv["second_half"]
                p1 = f"p={h1['p_value']:.4f}" if h1.get("p_value") is not None else "N/A"
                p2 = f"p={h2['p_value']:.4f}" if h2.get("p_value") is not None else "N/A"
                lines.append(f"    First half:  N={h1['n']}, {p1}")
                lines.append(f"    Second half: N={h2['n']}, {p2}")
                lines.append(
                    f"    Consistent: {'YES' if surv['consistent_edge'] else 'NO'}"
                )
            lines.append("")

            # Regime test
            regime = self.regime_test(from_s, to_s)
            if "error" not in regime:
                lines.append(f"  Regime test (median daily range: {regime['median_daily_range']:.5f}):")
                hv = regime["high_vol"]
                lv = regime["low_vol"]
                p_hv = f"p={hv['p_value']:.4f}" if hv.get("p_value") is not None else "N/A"
                p_lv = f"p={lv['p_value']:.4f}" if lv.get("p_value") is not None else "N/A"
                lines.append(f"    High-vol: N={hv['n']}, {p_hv}")
                lines.append(f"    Low-vol:  N={lv['n']}, {p_lv}")
                lines.append(
                    f"    Consistent: {'YES' if regime['consistent_edge'] else 'NO'}"
                )
            lines.append("")

        # Transaction cost analysis for any significant edges
        lines.append("=" * 80)
        lines.append("TRANSACTION COST ANALYSIS")
        lines.append("=" * 80)
        lines.append("Round-trip cost: $1.24 per contract (MNQ)")
        lines.append("MNQ point value: $2.00")
        lines.append("Minimum edge needed: 0.62 points (~0.003% at NQ 20000)")
        lines.append("")

        # Final verdict
        lines.append("=" * 80)
        if any_significant:
            lines.append("RESULT: STATISTICALLY SIGNIFICANT CELLS FOUND (p < 0.05)")
            lines.append("WARNING: Statistical significance ≠ trading edge.")
            lines.append("Check effect sizes, transaction costs, and survivorship tests above.")
        else:
            lines.append("NO EDGE FOUND — session handoffs are random")
            lines.append("No cell showed p < 0.05 against uniform distribution.")
        lines.append("=" * 80)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _load_csv(self, filepath: str) -> pd.DataFrame:
        """Load CSV, normalize to ET timezone-aware DatetimeIndex."""
        df = pd.read_csv(filepath)

        # Handle both timestamp formats
        if "timestamp" in df.columns:
            # Use utc=True to handle mixed EDT/EST offsets
            df["datetime"] = pd.to_datetime(df["timestamp"], utc=True)
        elif "time" in df.columns:
            df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
        else:
            raise ValueError(f"CSV must have 'timestamp' or 'time' column. Found: {list(df.columns)}")

        # Normalize column names
        col_map = {c: c.lower() for c in df.columns}
        df = df.rename(columns=col_map)

        # Convert to US/Eastern
        df["datetime"] = df["datetime"].dt.tz_convert("America/New_York")

        df = df.sort_values("datetime").reset_index(drop=True)

        # Add helper columns
        df["time_only"] = df["datetime"].dt.time
        df["date_only"] = df["datetime"].dt.date

        self._data_start = df["datetime"].iloc[0]
        self._data_end = df["datetime"].iloc[-1]
        self._df = df  # keep reference for selection bias test

        return df

    def _get_session_mask(self, df: pd.DataFrame, session: SessionName) -> pd.Series:
        """Return boolean mask for bars belonging to a session."""
        start_t, end_t = SESSION_TIMES[session]
        times = df["time_only"]

        if session == SessionName.ASIA:
            # Crosses midnight: 18:00 → 02:00
            return (times >= start_t) | (times < end_t)
        else:
            return (times >= start_t) & (times < end_t)

    def _extract_sessions(self, df: pd.DataFrame) -> None:
        """Extract session OHLCV stats for each trading day."""
        self.sessions = []

        # Group by trading date.
        # For Asia session, the trading date is the date of the 18:00 start.
        for session_name in SessionName:
            mask = self._get_session_mask(df, session_name)
            session_bars = df[mask].copy()

            if session_bars.empty:
                continue

            # Assign trading dates
            if session_name == SessionName.ASIA:
                # Bars from 18:00-23:59 belong to that calendar date;
                # bars from 00:00-01:59 belong to the previous calendar date
                session_bars = session_bars.copy()
                session_bars["trading_date"] = session_bars["datetime"].apply(
                    lambda dt: dt.date() if dt.time() >= time(18, 0) else (dt - timedelta(days=1)).date()
                )
            else:
                session_bars["trading_date"] = session_bars["date_only"]

            for tdate, group in session_bars.groupby("trading_date"):
                if len(group) < 5:  # skip days with very few bars
                    continue
                self.sessions.append(SessionStats(
                    session_name=session_name,
                    date=tdate,
                    open_price=float(group["open"].iloc[0]),
                    high_price=float(group["high"].max()),
                    low_price=float(group["low"].min()),
                    close_price=float(group["close"].iloc[-1]),
                    volume=int(group["volume"].sum()),
                    bar_count=len(group),
                ))

    def _compute_median_ranges(self) -> None:
        """Compute median range per session type for classification."""
        for sname in SessionName:
            ranges = [s.session_range for s in self.sessions if s.session_name == sname]
            self.median_ranges[sname] = float(np.median(ranges)) if ranges else 0.0

    def _classify_session(self, s: SessionStats) -> SessionBehavior:
        """Classify a session's behavior."""
        ret = s.session_return
        rng = s.session_range
        close_pos = s.close_position_in_range
        median_rng = self.median_ranges.get(s.session_name, 0.0)

        # Expansion check first (high volatility)
        if median_rng > 0 and rng > 1.5 * median_rng:
            return SessionBehavior.EXPANSION

        # Spike reversal: wide range but close near open
        if rng > 0.004 and abs(ret) < 0.001:
            return SessionBehavior.SPIKE_REVERSAL

        # Strong trends
        if ret > 0.003 and close_pos > 0.8:
            return SessionBehavior.STRONG_TREND_UP
        if ret < -0.003 and close_pos < 0.2:
            return SessionBehavior.STRONG_TREND_DOWN

        # Weak trends
        if ret > 0.001:
            return SessionBehavior.WEAK_TREND_UP
        if ret < -0.001:
            return SessionBehavior.WEAK_TREND_DOWN

        # Range-bound
        if abs(ret) <= 0.001 and rng < median_rng:
            return SessionBehavior.RANGE_BOUND

        # Default: classify by direction
        if ret > 0:
            return SessionBehavior.WEAK_TREND_UP
        elif ret < 0:
            return SessionBehavior.WEAK_TREND_DOWN
        else:
            return SessionBehavior.RANGE_BOUND

    def _classify_all_sessions(self) -> None:
        """Attach behavior classification to all sessions."""
        self._session_behaviors: dict[tuple, SessionBehavior] = {}
        for s in self.sessions:
            behavior = self._classify_session(s)
            self._session_behaviors[(s.session_name, s.date)] = behavior

    def _classify_handoff(self, from_return: float, to_return: float) -> HandoffOutcome:
        """Classify handoff outcome based on directional relationship."""
        # Range: next session stays within 0.1% of previous close
        if abs(to_return) < 0.001:
            return HandoffOutcome.RANGE

        # Continuation: same direction
        if from_return > 0 and to_return > 0:
            return HandoffOutcome.CONTINUATION
        if from_return < 0 and to_return < 0:
            return HandoffOutcome.CONTINUATION

        # Reversal: opposite direction with > 0.15% against
        if from_return > 0 and to_return < -0.0015:
            return HandoffOutcome.REVERSAL
        if from_return < 0 and to_return > 0.0015:
            return HandoffOutcome.REVERSAL

        # Mild counter-move → classify as RANGE
        return HandoffOutcome.RANGE

    def _build_handoff_observations(self) -> None:
        """Build handoff observations for all consecutive session pairs."""
        self.observations = []

        # Index sessions by (name, date) for quick lookup
        session_map: dict[tuple, SessionStats] = {}
        for s in self.sessions:
            session_map[(s.session_name, s.date)] = s

        for from_name, to_name in HANDOFF_PAIRS:
            # Find matching dates
            from_dates = {s.date for s in self.sessions if s.session_name == from_name}
            to_dates = {s.date for s in self.sessions if s.session_name == to_name}

            for d in sorted(from_dates):
                # For same-day sessions or next-day (Asia → London)
                # Asia on date D → London on date D+1 (because Asia is evening of D)
                if from_name == SessionName.ASIA:
                    to_date = d + timedelta(days=1)
                else:
                    to_date = d

                # For skip-session pairs
                if from_name == SessionName.ASIA and to_name == SessionName.NY_OPEN:
                    to_date = d + timedelta(days=1)

                from_s = session_map.get((from_name, d))
                to_s = session_map.get((to_name, to_date))

                if from_s is None or to_s is None:
                    continue

                from_behavior = self._session_behaviors.get((from_name, d))
                to_behavior = self._session_behaviors.get((to_name, to_date))

                if from_behavior is None or to_behavior is None:
                    continue

                outcome = self._classify_handoff(from_s.session_return, to_s.session_return)

                self.observations.append(HandoffObservation(
                    date=d,
                    from_session=from_name,
                    to_session=to_name,
                    from_behavior=from_behavior,
                    to_behavior=to_behavior,
                    from_return=from_s.session_return,
                    to_return=to_s.session_return,
                    outcome=outcome,
                ))

    def run_selection_bias_test(self) -> dict:
        """Run selection bias test using stored DataFrame."""
        if hasattr(self, "_df"):
            return self.selection_bias_test(self._df)
        return {"error": "No data loaded. Call analyze_csv() first."}
