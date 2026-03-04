"""
Intraday Session Profiler
==========================
Tracks intraday session phases for MNQ/NQ futures and provides
position sizing and stop width modifiers per phase.

Session Phases (all times Eastern):
  PRE_MARKET:    04:00-09:30  (Globex active, no trading)
  OPENING_DRIVE: 09:30-10:00  (institutional order clustering)
  IB_PERIOD:     09:30-10:30  (initial balance window)
  MORNING:       10:30-12:00  (post-IB continuation)
  LUNCH:         12:00-13:30  (low volume, wide spreads)
  AFTERNOON:     13:30-15:00  (volume recovery)
  MOC_WINDOW:    15:00-15:45  (institutional rebalancing flow)
  CLOSE:         15:45-16:00  (final minutes, avoid new entries)
  POST_MARKET:   16:00-18:00  (thin liquidity, no trading)

Reference: Admati & Pfleiderer (1988), Heston et al. (2010)
"""

import logging
from datetime import datetime, time
from typing import Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Session phase boundaries (ET times, inclusive start, exclusive end)
_PHASE_BOUNDARIES = [
    ("PRE_MARKET",    time(4, 0),   time(9, 30)),
    ("OPENING_DRIVE", time(9, 30),  time(10, 0)),
    ("IB_PERIOD",     time(10, 0),  time(10, 30)),
    ("MORNING",       time(10, 30), time(12, 0)),
    ("LUNCH",         time(12, 0),  time(13, 30)),
    ("AFTERNOON",     time(13, 30), time(15, 0)),
    ("MOC_WINDOW",    time(15, 0),  time(15, 45)),
    ("CLOSE",         time(15, 45), time(16, 0)),
    ("POST_MARKET",   time(16, 0),  time(18, 0)),
]

# Phase modifiers: position_size_mult, stop_width_mult
_PHASE_MODIFIERS: Dict[str, Dict[str, float]] = {
    "PRE_MARKET":    {"position_size_mult": 0.0, "stop_width_mult": 1.0},
    "OPENING_DRIVE": {"position_size_mult": 1.2, "stop_width_mult": 1.1},
    "IB_PERIOD":     {"position_size_mult": 1.0, "stop_width_mult": 1.0},
    "MORNING":       {"position_size_mult": 1.1, "stop_width_mult": 1.0},
    "LUNCH":         {"position_size_mult": 0.7, "stop_width_mult": 0.8},
    "AFTERNOON":     {"position_size_mult": 1.0, "stop_width_mult": 1.0},
    "MOC_WINDOW":    {"position_size_mult": 1.15, "stop_width_mult": 1.1},
    "CLOSE":         {"position_size_mult": 0.5, "stop_width_mult": 0.7},
    "POST_MARKET":   {"position_size_mult": 0.0, "stop_width_mult": 1.0},
}


class SessionProfiler:
    """
    Tracks intraday session phases and returns sizing/stop modifiers.

    Usage:
        profiler = SessionProfiler()
        phase = profiler.get_session_phase(bar.timestamp)
        mods = profiler.get_phase_modifier(phase)
        adjusted_size = base_size * mods["position_size_mult"]
        adjusted_stop = base_stop * mods["stop_width_mult"]
    """

    def get_session_phase(self, timestamp: datetime) -> str:
        """
        Determine the current session phase from a timezone-aware timestamp.

        Args:
            timestamp: Timezone-aware datetime.

        Returns:
            Phase name string (e.g. "OPENING_DRIVE", "LUNCH").
            Returns "CLOSED" if outside all defined phases.
        """
        et_time = timestamp.astimezone(ET).time()

        for phase_name, start, end in _PHASE_BOUNDARIES:
            if start <= et_time < end:
                return phase_name

        return "CLOSED"

    def get_phase_modifier(self, phase: str) -> Dict[str, float]:
        """
        Return position sizing and stop width multipliers for a phase.

        Args:
            phase: Phase name from get_session_phase().

        Returns:
            Dict with "position_size_mult" and "stop_width_mult".
            Defaults to no-trade (position=0.0) for unknown phases.
        """
        return _PHASE_MODIFIERS.get(phase, {
            "position_size_mult": 0.0,
            "stop_width_mult": 1.0,
        })

    def is_rth(self, timestamp: datetime) -> bool:
        """Check if timestamp falls within Regular Trading Hours (9:30-16:00 ET)."""
        et_time = timestamp.astimezone(ET).time()
        return time(9, 30) <= et_time < time(16, 0)

    def allows_new_entries(self, timestamp: datetime) -> bool:
        """Check if the current phase allows new trade entries."""
        phase = self.get_session_phase(timestamp)
        mods = self.get_phase_modifier(phase)
        return mods["position_size_mult"] > 0.0

    def get_session_info(self, timestamp: datetime) -> Dict:
        """
        Return full session context for a given timestamp.

        Returns:
            Dict with phase, modifiers, is_rth, and allows_entries.
        """
        phase = self.get_session_phase(timestamp)
        mods = self.get_phase_modifier(phase)
        return {
            "phase": phase,
            "position_size_mult": mods["position_size_mult"],
            "stop_width_mult": mods["stop_width_mult"],
            "is_rth": self.is_rth(timestamp),
            "allows_new_entries": mods["position_size_mult"] > 0.0,
        }
