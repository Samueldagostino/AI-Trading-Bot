"""
Instrument Specifications
==========================
Defines InstrumentSpec for CME Micro futures:
  MNQ (Micro Nasdaq-100), MES (Micro S&P 500),
  MYM (Micro Dow), M2K (Micro Russell 2000)

Each spec captures tick_size, point_value, margins, session times,
and contract cycle — everything needed to trade an instrument
without hardcoding.

Usage:
    from config.instruments import InstrumentSpec
    spec = InstrumentSpec.from_symbol("MNQ")
    risk_dollars = stop_points * spec.point_value * contracts
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class InstrumentSpec:
    """Complete specification for a single CME futures instrument."""

    symbol: str                   # "MNQ", "MES", "MYM", "M2K"
    full_name: str                # "Micro E-mini Nasdaq-100"
    exchange: str                 # "CME"
    tick_size: float              # Minimum price increment in points
    tick_value: float             # Dollar value of one tick
    point_value: float            # Dollar value of one full point
    margin_requirement: float     # Approximate initial margin per contract
    typical_spread: float         # Typical bid-ask spread in ticks
    min_volume: int               # Minimum daily volume for tradability
    session_open: str             # RTH open time ET (e.g., "09:30")
    session_close: str            # RTH close time ET (e.g., "16:00")
    expiry_cycle: str             # "HMUZ" for quarterly futures
    contract_months: Dict[str, str] = field(default_factory=dict)
    commission_per_contract: float = 1.29  # Default Tradovate commission

    @property
    def ticks_per_point(self) -> float:
        """Number of ticks in one full point."""
        if self.tick_size <= 0:
            return 1.0
        return round(1.0 / self.tick_size, 6)

    def points_to_dollars(self, points: float, contracts: int = 1) -> float:
        """Convert price movement in points to dollar P&L."""
        return round(points * self.point_value * contracts, 2)

    def dollars_to_points(self, dollars: float, contracts: int = 1) -> float:
        """Convert dollar amount to points equivalent."""
        if self.point_value <= 0 or contracts <= 0:
            return 0.0
        return round(dollars / (self.point_value * contracts), 2)

    def round_to_tick(self, price: float) -> float:
        """Round a price to the nearest valid tick."""
        if self.tick_size <= 0:
            return price
        return round(round(price / self.tick_size) * self.tick_size, 6)

    @staticmethod
    def from_symbol(symbol: str) -> "InstrumentSpec":
        """
        Factory method: get InstrumentSpec by symbol.

        Args:
            symbol: One of "MNQ", "MES", "MYM", "M2K" (case-insensitive)

        Returns:
            InstrumentSpec for the requested instrument.

        Raises:
            ValueError: If symbol is not supported.
        """
        key = symbol.upper().strip()
        if key not in INSTRUMENT_SPECS:
            supported = ", ".join(sorted(INSTRUMENT_SPECS.keys()))
            raise ValueError(
                f"Unsupported instrument '{symbol}'. Supported: {supported}"
            )
        return INSTRUMENT_SPECS[key]

    @staticmethod
    def supported_symbols() -> list:
        """Return list of all supported instrument symbols."""
        return sorted(INSTRUMENT_SPECS.keys())


# ================================================================
# Pre-defined instrument specifications
# ================================================================

_QUARTERLY_MONTHS = {
    "H": "March",
    "M": "June",
    "U": "September",
    "Z": "December",
}

INSTRUMENT_SPECS: Dict[str, InstrumentSpec] = {
    "MNQ": InstrumentSpec(
        symbol="MNQ",
        full_name="Micro E-mini Nasdaq-100",
        exchange="CME",
        tick_size=0.25,
        tick_value=0.50,
        point_value=2.0,
        margin_requirement=2_100.0,
        typical_spread=1.0,        # ~1 tick typical
        min_volume=1_000,
        session_open="09:30",
        session_close="16:00",
        expiry_cycle="HMUZ",
        contract_months=_QUARTERLY_MONTHS,
        commission_per_contract=1.29,
    ),
    "MES": InstrumentSpec(
        symbol="MES",
        full_name="Micro E-mini S&P 500",
        exchange="CME",
        tick_size=0.25,
        tick_value=1.25,
        point_value=5.0,
        margin_requirement=1_500.0,
        typical_spread=1.0,
        min_volume=1_000,
        session_open="09:30",
        session_close="16:00",
        expiry_cycle="HMUZ",
        contract_months=_QUARTERLY_MONTHS,
        commission_per_contract=1.29,
    ),
    "MYM": InstrumentSpec(
        symbol="MYM",
        full_name="Micro E-mini Dow Jones",
        exchange="CME",
        tick_size=1.0,
        tick_value=0.50,
        point_value=0.50,
        margin_requirement=1_100.0,
        typical_spread=1.0,
        min_volume=500,
        session_open="09:30",
        session_close="16:00",
        expiry_cycle="HMUZ",
        contract_months=_QUARTERLY_MONTHS,
        commission_per_contract=1.29,
    ),
    "M2K": InstrumentSpec(
        symbol="M2K",
        full_name="Micro E-mini Russell 2000",
        exchange="CME",
        tick_size=0.10,
        tick_value=0.50,
        point_value=5.0,
        margin_requirement=800.0,
        typical_spread=1.0,
        min_volume=500,
        session_open="09:30",
        session_close="16:00",
        expiry_cycle="HMUZ",
        contract_months=_QUARTERLY_MONTHS,
        commission_per_contract=1.29,
    ),
}


def get_instrument(symbol: str) -> InstrumentSpec:
    """Convenience alias for InstrumentSpec.from_symbol()."""
    return InstrumentSpec.from_symbol(symbol)


def print_all_specs() -> None:
    """Print a formatted table of all supported instruments."""
    print(f"\n{'Symbol':<8} {'Name':<30} {'Tick':<6} {'$/Tick':<8} {'$/Pt':<8} {'Margin':<10}")
    print("-" * 78)
    for sym in sorted(INSTRUMENT_SPECS):
        s = INSTRUMENT_SPECS[sym]
        print(f"{s.symbol:<8} {s.full_name:<30} {s.tick_size:<6.2f} ${s.tick_value:<7.2f} ${s.point_value:<7.2f} ${s.margin_requirement:>8,.0f}")
    print()
