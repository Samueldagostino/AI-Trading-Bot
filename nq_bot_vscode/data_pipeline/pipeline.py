"""
Data Pipeline
==============
Ingests NQ price data from multiple sources:

1. Tradovate API: Live + historical bars (primary)
2. TradingView CSV Export: Manual export for backtesting (fallback)

TradingView Integration Notes:
- TradingView does NOT have a public API for automated data export
- WORKAROUND: Export chart data as CSV from TradingView manually
  (Chart -> ... menu -> Export chart data)
- This module parses those CSV files for backtesting
- For live trading, Tradovate WebSocket provides the data feed

How to export from TradingView:
1. Open NQ / MNQ chart in TradingView
2. Set to 1-minute timeframe
3. Click the "..." menu on the chart -> "Export chart data"
4. Save CSV to data/tradingview/ directory
5. This module reads it automatically
"""

import csv
import logging
import math
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Iterator, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Map CSV filename minute-intervals to standard timeframe labels
MINUTES_TO_LABEL: Dict[int, str] = {
    1: "1m", 2: "2m", 3: "3m", 5: "5m", 15: "15m",
    30: "30m", 60: "1H", 240: "4H", 1440: "1D",
}


def _parse_tf_from_filename(filename: str) -> Optional[str]:
    """Parse timeframe label from TradingView CSV filename.

    Handles patterns like:
        CME_MINI_MNQ1!, 2m.csv      -> "2m"
        CME_MINI_MNQ1!, 60 1hr.csv  -> "1H"
        CME_MINI_MNQ1!, 240 4hr.csv -> "4H"
        CME_MINI_MNQ1!, 1D.csv      -> "1D"
    """
    stem = Path(filename).stem
    parts = stem.rsplit(", ", 1)
    if len(parts) < 2:
        parts = stem.rsplit(",", 1)
    if len(parts) < 2:
        return None
    tf_part = parts[1].strip()

    # "1D" daily
    if tf_part.upper() == "1D":
        return "1D"

    # "2m", "5m", "15m", "30m" etc
    m = re.match(r'^(\d+)m$', tf_part, re.IGNORECASE)
    if m:
        minutes = int(m.group(1))
        return MINUTES_TO_LABEL.get(minutes)

    # "60 1hr", "240 4hr" — leading number is the minute interval
    m = re.match(r'^(\d+)\s', tf_part)
    if m:
        minutes = int(m.group(1))
        return MINUTES_TO_LABEL.get(minutes)

    return None


def bardata_to_bar(bar_data: 'BarData') -> 'Bar':
    """Convert BarData (pipeline format) to Bar (feature engine format)."""
    from features.engine import Bar
    return Bar(
        timestamp=bar_data.timestamp,
        open=bar_data.open,
        high=bar_data.high,
        low=bar_data.low,
        close=bar_data.close,
        volume=bar_data.volume,
        bid_volume=bar_data.bid_volume,
        ask_volume=bar_data.ask_volume,
        delta=bar_data.delta,
        tick_count=bar_data.tick_count,
        vwap=bar_data.vwap,
    )


def bardata_to_htfbar(bar_data: 'BarData') -> 'HTFBar':
    """Convert BarData (pipeline format) to HTFBar (HTF bias engine format)."""
    from features.htf_engine import HTFBar
    return HTFBar(
        timestamp=bar_data.timestamp,
        open=bar_data.open,
        high=bar_data.high,
        low=bar_data.low,
        close=bar_data.close,
        volume=bar_data.volume,
    )


class MultiTimeframeIterator:
    """Iterates over bars from multiple timeframes in chronological order.

    Yields (timeframe_label, BarData) tuples sorted by timestamp.
    When timestamps tie, higher timeframes come first so HTF bias
    is updated before execution-TF bars are processed.
    """

    _TF_PRIORITY = {
        "1D": 0, "4H": 1, "1H": 2, "30m": 3,
        "15m": 4, "5m": 5, "3m": 6, "2m": 7, "1m": 8,
    }

    def __init__(self, merged: List[Tuple[str, 'BarData']]):
        self._items = sorted(
            merged,
            key=lambda x: (x[1].timestamp, self._TF_PRIORITY.get(x[0], 999)),
        )

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Tuple[str, 'BarData']]:
        return iter(self._items)


@dataclass
class BarData:
    """Universal bar format used across the system."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    bid_volume: int = 0
    ask_volume: int = 0
    delta: int = 0
    tick_count: int = 0
    vwap: float = 0.0
    source: str = ""      # "tradovate", "tradingview", "sample"


class TradingViewImporter:
    """
    Parses TradingView CSV exports into BarData objects.
    
    TradingView CSV format (typical):
    time,open,high,low,close,Volume
    2025-01-02T14:30:00Z,20150.25,20155.50,20148.00,20153.75,8234
    
    Notes:
    - Column names may vary by TV version
    - Timestamps may be in local time or UTC
    - Volume may not include bid/ask split (no delta available)
    """

    # Known TradingView CSV column name variations
    TIME_COLUMNS = ["time", "date", "datetime", "timestamp", "Date", "Time"]
    OPEN_COLUMNS = ["open", "Open", "o"]
    HIGH_COLUMNS = ["high", "High", "h"]
    LOW_COLUMNS = ["low", "Low", "l"]
    CLOSE_COLUMNS = ["close", "Close", "c"]
    VOLUME_COLUMNS = ["volume", "Volume", "vol", "Vol", "v"]

    def __init__(self, config=None):
        self.config = config
        self._import_dir = config.data_pipeline.tv_export_directory if config else "./data/tradingview"

    def import_file(self, filepath: str) -> List[BarData]:
        """
        Import a single TradingView CSV file.
        
        Args:
            filepath: Path to the CSV file
            
        Returns:
            List of BarData objects in chronological order
        """
        filepath = Path(filepath)
        if not filepath.exists():
            logger.error(f"File not found: {filepath}")
            return []

        logger.info(f"Importing TradingView CSV: {filepath}")
        bars = []

        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                # Detect delimiter
                sample = f.read(2048)
                f.seek(0)
                
                delimiter = ","
                if "\t" in sample and "," not in sample:
                    delimiter = "\t"

                reader = csv.DictReader(f, delimiter=delimiter)
                headers = reader.fieldnames

                if not headers:
                    logger.error(f"No headers found in {filepath}")
                    return []

                # Map columns
                col_map = self._map_columns(headers)
                if not all(k in col_map for k in ["time", "open", "high", "low", "close"]):
                    logger.error(f"Missing required columns. Found: {headers}")
                    logger.error(f"Mapped: {col_map}")
                    return []

                for row_num, row in enumerate(reader, start=2):
                    try:
                        bar = self._parse_row(row, col_map, row_num)
                        if bar:
                            bars.append(bar)
                    except Exception as e:
                        if row_num < 5:  # Only log first few errors
                            logger.warning(f"Row {row_num} parse error: {e}")
                        continue

        except Exception as e:
            logger.error(f"Error reading {filepath}: {e}")
            return []

        # Sort chronologically
        bars.sort(key=lambda b: b.timestamp)
        
        logger.info(f"Imported {len(bars)} bars from {filepath}")
        if bars:
            logger.info(f"  Date range: {bars[0].timestamp} -> {bars[-1].timestamp}")
            logger.info(f"  Price range: {min(b.low for b in bars):.2f} - {max(b.high for b in bars):.2f}")

        return bars

    def import_directory(self, directory: str = None) -> List[BarData]:
        """Import all CSV files from a directory, merge and sort."""
        dir_path = Path(directory or self._import_dir)
        if not dir_path.exists():
            logger.warning(f"Import directory does not exist: {dir_path}")
            return []

        all_bars = []
        csv_files = sorted(dir_path.glob("*.csv"))
        
        if not csv_files:
            logger.warning(f"No CSV files found in {dir_path}")
            return []

        for csv_file in csv_files:
            bars = self.import_file(str(csv_file))
            all_bars.extend(bars)

        # Deduplicate by timestamp
        seen = set()
        unique_bars = []
        for bar in sorted(all_bars, key=lambda b: b.timestamp):
            key = bar.timestamp.isoformat()
            if key not in seen:
                seen.add(key)
                unique_bars.append(bar)

        logger.info(f"Total imported: {len(unique_bars)} unique bars from {len(csv_files)} files")
        return unique_bars

    def _map_columns(self, headers: list) -> dict:
        """Map CSV column names to our standard names."""
        col_map = {}
        headers_lower = {h.lower().strip(): h for h in headers}

        for std_name, variations in [
            ("time", self.TIME_COLUMNS),
            ("open", self.OPEN_COLUMNS),
            ("high", self.HIGH_COLUMNS),
            ("low", self.LOW_COLUMNS),
            ("close", self.CLOSE_COLUMNS),
            ("volume", self.VOLUME_COLUMNS),
        ]:
            for var in variations:
                if var.lower() in headers_lower:
                    col_map[std_name] = headers_lower[var.lower()]
                    break

        return col_map

    def _parse_row(self, row: dict, col_map: dict, row_num: int) -> Optional[BarData]:
        """Parse a single CSV row into a BarData."""
        time_str = row.get(col_map["time"], "").strip()
        if not time_str:
            return None

        # Parse timestamp (handle multiple formats)
        timestamp = self._parse_timestamp(time_str)
        if not timestamp:
            return None

        try:
            open_price = float(row[col_map["open"]])
            high = float(row[col_map["high"]])
            low = float(row[col_map["low"]])
            close = float(row[col_map["close"]])
        except (ValueError, KeyError):
            return None

        volume = 0
        if "volume" in col_map:
            try:
                vol_str = row.get(col_map["volume"], "0")
                volume = int(float(vol_str))
            except (ValueError, TypeError):
                volume = 0

        # NaN/Inf guard — reject corrupted price data
        if not (math.isfinite(open_price) and math.isfinite(high) and
                math.isfinite(low) and math.isfinite(close)):
            return None

        # Basic sanity checks
        if high < low or open_price <= 0:
            return None

        return BarData(
            timestamp=timestamp,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source="tradingview",
        )

    def _parse_timestamp(self, time_str: str) -> Optional[datetime]:
        """Parse timestamp string in various formats."""
        formats = [
            "%Y-%m-%dT%H:%M:%SZ",          # ISO UTC
            "%Y-%m-%dT%H:%M:%S%z",          # ISO with tz
            "%Y-%m-%dT%H:%M:%S",            # ISO no tz
            "%Y-%m-%d %H:%M:%S",            # Standard
            "%Y-%m-%d %H:%M",               # No seconds
            "%m/%d/%Y %H:%M:%S",            # US format
            "%m/%d/%Y %H:%M",               # US no seconds
            "%d/%m/%Y %H:%M:%S",            # EU format
            "%Y%m%d %H%M%S",                # Compact
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

        # Try pandas-style if nothing else works
        try:
            # Handle Unix timestamp
            ts = float(time_str)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            pass

        return None


class DataPipeline:
    """
    Main data pipeline coordinating all data sources.
    
    For backtesting: TradingView CSV -> BarData -> Feature Engine
    For live trading: Tradovate WebSocket -> BarData -> Feature Engine
    """

    def __init__(self, config, db_manager=None):
        self.config = config
        self.db = db_manager
        self.tv_importer = TradingViewImporter(config)
        self._bar_buffer: List[BarData] = []

    async def load_backtest_data(self, source: str = "tradingview", filepath: str = None) -> List[BarData]:
        """
        Load historical data for backtesting.
        
        Args:
            source: "tradingview" or "tradovate"
            filepath: Specific file to load (for TV), or None to scan directory
        """
        if source == "tradingview":
            if filepath:
                bars = self.tv_importer.import_file(filepath)
            else:
                bars = self.tv_importer.import_directory()
            
            if not bars:
                logger.warning("No TradingView data loaded. Check data/tradingview/ directory.")
            return bars

        elif source == "tradovate":
            logger.info("Tradovate historical data requires active connection")
            # Would fetch via tradovate_client.get_historical_bars()
            return []

        elif source == "sample":
            logger.info("Generating sample data for testing...")
            from scripts.run_backtest import generate_sample_bars
            sample_bars = generate_sample_bars(3000)
            return [
                BarData(
                    timestamp=b.timestamp, open=b.open, high=b.high,
                    low=b.low, close=b.close, volume=b.volume,
                    bid_volume=b.bid_volume, ask_volume=b.ask_volume,
                    delta=b.delta, source="sample",
                )
                for b in sample_bars
            ]

        return []

    def convert_to_feature_bars(self, data: List[BarData]) -> list:
        """Convert BarData list to Bar objects for the feature engine."""
        from features.engine import Bar
        return [
            Bar(
                timestamp=d.timestamp, open=d.open, high=d.high,
                low=d.low, close=d.close, volume=d.volume,
                bid_volume=d.bid_volume, ask_volume=d.ask_volume,
                delta=d.delta, tick_count=d.tick_count, vwap=d.vwap,
            )
            for d in data
        ]

    async def store_bars(self, bars: List[BarData]) -> int:
        """Store bars to PostgreSQL."""
        if not self.db:
            return 0

        stored = 0
        for bar in bars:
            try:
                await self.db.execute(
                    """INSERT INTO nq_bars_1m 
                    (timestamp_utc, symbol, contract, open, high, low, close, 
                     volume, bid_volume, ask_volume, delta)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT DO NOTHING""",
                    bar.timestamp, "MNQ", self.config.tradovate.symbol,
                    bar.open, bar.high, bar.low, bar.close,
                    bar.volume, bar.bid_volume, bar.ask_volume, bar.delta,
                )
                stored += 1
            except Exception as e:
                logger.error(f"Failed to store bar: {e}")

        logger.info(f"Stored {stored}/{len(bars)} bars to database")
        return stored

    def get_data_summary(self, bars: List[BarData]) -> dict:
        """Summarize loaded data for verification."""
        if not bars:
            return {"status": "no_data"}

        prices = [b.close for b in bars]
        volumes = [b.volume for b in bars]
        
        return {
            "bar_count": len(bars),
            "date_range_start": bars[0].timestamp.isoformat(),
            "date_range_end": bars[-1].timestamp.isoformat(),
            "trading_days": len(set(b.timestamp.date() for b in bars)),
            "price_min": round(min(b.low for b in bars), 2),
            "price_max": round(max(b.high for b in bars), 2),
            "price_current": round(bars[-1].close, 2),
            "avg_volume": round(sum(volumes) / len(volumes)),
            "total_volume": sum(volumes),
            "source": bars[0].source,
            "has_delta": any(b.delta != 0 for b in bars),
        }

    # ================================================================
    # MULTI-TIMEFRAME METHODS
    # ================================================================
    def load_mtf_data(self, directory: str = None) -> Dict[str, List[BarData]]:
        """Load all TradingView CSVs, detect timeframe from filename, group by TF."""
        dir_path = Path(directory or self.tv_importer._import_dir)
        if not dir_path.exists():
            logger.warning(f"MTF data directory not found: {dir_path}")
            return {}

        tf_bars: Dict[str, List[BarData]] = {}

        for csv_file in sorted(dir_path.glob("*.csv")):
            tf_label = _parse_tf_from_filename(str(csv_file))
            if not tf_label:
                logger.warning(f"Could not determine timeframe for: {csv_file.name}")
                continue

            bars = self.tv_importer.import_file(str(csv_file))
            if bars:
                tf_bars[tf_label] = bars
                logger.info(f"  {tf_label}: {len(bars)} bars from {csv_file.name}")

        return tf_bars

    def get_mtf_summary(self, tf_bars: Dict[str, List[BarData]]) -> dict:
        """Summarize multi-timeframe data for display."""
        summary = {}
        for tf, bars in sorted(tf_bars.items()):
            if bars:
                summary[tf] = {
                    "bars": len(bars),
                    "price_range": f"{min(b.low for b in bars):.2f} - {max(b.high for b in bars):.2f}",
                    "date_range": f"{bars[0].timestamp.date()} to {bars[-1].timestamp.date()}",
                }
        return summary

    def create_mtf_iterator(self, tf_bars: Dict[str, List[BarData]]) -> MultiTimeframeIterator:
        """Create a chronologically sorted iterator across all timeframes."""
        merged: List[Tuple[str, BarData]] = []
        for tf, bars in tf_bars.items():
            for bar in bars:
                merged.append((tf, bar))

        tf_priority = MultiTimeframeIterator._TF_PRIORITY
        merged.sort(key=lambda x: (x[1].timestamp, tf_priority.get(x[0], 99)))

        return MultiTimeframeIterator(merged)
