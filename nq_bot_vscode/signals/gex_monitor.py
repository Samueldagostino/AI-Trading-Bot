"""
GEX Monitor — IBKR Options Chain + Quant Data API Fallback
============================================================
Computes Gamma Exposure (GEX) from IBKR SPX options chain data.
Falls back to Quant Data API when IBKR is unavailable.
When neither is available, operates in MOCK mode with realistic
synthetic data for dry-run testing.

GEX computation (naive):
  Per-strike GEX = gamma * OI * 100 * spot^2 * 0.01
  Net GEX = sum(call GEX) - sum(put GEX) across strikes within 5% of spot

GEX regime classification drives the gamma modifier:
  - STRONG_POSITIVE:  dealers dampen moves   -> reduce size (0.80x)
  - MILD_POSITIVE:    slight dampening       -> slightly reduce (0.95x)
  - MILD_NEGATIVE:    trends extend          -> slightly increase (1.10x)
  - STRONG_NEGATIVE:  momentum amplified     -> increase size (1.25x)
  - EXTREME_NEGATIVE: protect capital        -> reduce size (0.75x)

On ANY error: log warning, return safe defaults. Never crash the bot.
"""

import json
import logging
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Import config
try:
    from config.quantdata_config import (
        QUANTDATA_API_BASE,
        QUANTDATA_ENDPOINTS,
        TOKEN_FILE,
        INSTANCE_ID_FILE,
        REFRESH_INTERVAL_IDLE,
        REFRESH_INTERVAL_ACTIVE,
        REFRESH_INTERVAL_PREFLIGHT,
        TICKERS,
        STRIKE_RANGE_PCT,
        NEAREST_EXPIRATIONS,
    )
except ImportError:
    QUANTDATA_API_BASE = "https://core-lb-prod.quantdata.us"
    QUANTDATA_ENDPOINTS = {
        "heat_map": "/api/options/heat-map/",
        "exposure_strike": "/api/options/exposure/strike/",
        "expirations": "/api/options/expirations",
    }
    TOKEN_FILE = "config/quantdata_token.txt"
    INSTANCE_ID_FILE = "config/quantdata_instance_id.txt"
    REFRESH_INTERVAL_IDLE = 300
    REFRESH_INTERVAL_ACTIVE = 120
    REFRESH_INTERVAL_PREFLIGHT = 60
    TICKERS = ["SPY", "QQQ"]
    STRIKE_RANGE_PCT = 0.05
    NEAREST_EXPIRATIONS = 4

_URGENCY_INTERVALS = {
    "idle": REFRESH_INTERVAL_IDLE,
    "active": REFRESH_INTERVAL_ACTIVE,
    "preflight": REFRESH_INTERVAL_PREFLIGHT,
}


# ── Regime modifier mappings ────────────────────────────────────
REGIME_MODIFIERS = {
    "STRONG_POSITIVE":  {"value": 0.80, "template": "Positive GEX {display}, dealers dampening"},
    "MILD_POSITIVE":    {"value": 0.95, "template": "Mild positive GEX {display}"},
    "MILD_NEGATIVE":    {"value": 1.10, "template": "Mild negative GEX {display}, trends may extend"},
    "STRONG_NEGATIVE":  {"value": 1.25, "template": "Strong negative GEX {display}, momentum amplified"},
    "EXTREME_NEGATIVE": {"value": 0.75, "template": "EXTREME negative GEX {display}, protect capital"},
    "UNKNOWN":          {"value": 1.00, "template": "GEX data unavailable"},
}


def _format_gex_display(net_gex: float) -> str:
    """Format net GEX as human-readable string like '-$15.2B'."""
    abs_val = abs(net_gex)
    sign = "-" if net_gex < 0 else ""
    if abs_val >= 1e9:
        return f"{sign}${abs_val / 1e9:.1f}B"
    elif abs_val >= 1e6:
        return f"{sign}${abs_val / 1e6:.1f}M"
    else:
        return f"{sign}${abs_val:,.0f}"


class GEXMonitor:
    """Computes Gamma Exposure from IBKR options chain or Quant Data API."""

    def __init__(
        self,
        token_file: str = TOKEN_FILE,
        instance_id_file: str = INSTANCE_ID_FILE,
        log_dir: Optional[str] = None,
        ibkr_client=None,
    ):
        self._token = self._load_file(token_file)
        self._instance_id = self._load_file(instance_id_file)
        self._last_update: Optional[float] = None
        self._last_result: Optional[dict] = None
        self._enabled = (
            self._token != "PASTE_YOUR_TOKEN_HERE"
            and self._token != ""
        )
        self._mock_cycle = 0
        self._ibkr_client = ibkr_client

        # Log directory for gamma_levels.json
        if log_dir is None:
            log_dir = str(Path(__file__).resolve().parent.parent / "logs")
        self._log_dir = log_dir
        self._gamma_log_path = Path(log_dir) / "gamma_levels.json"
        Path(log_dir).mkdir(parents=True, exist_ok=True)

    def set_ibkr_client(self, ibkr_client) -> None:
        """Set or update the IBKR client reference for options chain queries."""
        self._ibkr_client = ibkr_client

    @property
    def enabled(self) -> bool:
        return self._enabled or self._ibkr_client is not None

    @staticmethod
    def _load_file(path: str) -> str:
        """Read a config file, strip whitespace. Return empty string on error."""
        try:
            resolved = Path(path)
            if not resolved.is_absolute():
                # Try relative to project root
                project_root = Path(__file__).resolve().parent.parent
                resolved = project_root / path
                if not resolved.exists():
                    # Try from repo root
                    resolved = project_root.parent / path
            if resolved.exists():
                return resolved.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("Failed to read %s: %s", path, e)
        return ""

    def _build_headers(self) -> dict:
        """Build HTTP headers for Quant Data API requests."""
        return {
            "Authorization": self._token,
            "X-Instance-Id": self._instance_id,
            "X-Qd-Version": "1",
            "Accept": "application/json",
            "Origin": "https://v3.quantdata.us",
        }

    # ── IBKR Options Chain GEX ──────────────────────────────────

    def fetch_gex_from_ibkr(self) -> Optional[dict]:
        """
        Fetch SPX options chain from IBKR and compute naive GEX locally.

        GEX per strike = gamma * OI * 100 * spot^2 * 0.01
        Net GEX = sum(call GEX) - sum(put GEX) across strikes within 5% of spot.
        """
        if not self._ibkr_client:
            return None

        try:
            ib = self._ibkr_client._ib
            if not ib.isConnected():
                logger.warning("IBKR not connected — skipping GEX from options chain")
                return None

            from ib_insync import Index, Option

            # Request SPX spot price
            spx = Index("SPX", "CBOE")
            ib.qualifyContracts(spx)
            [ticker] = ib.reqTickers(spx)
            spot = ticker.marketPrice()
            if not spot or not math.isfinite(spot) or spot <= 0:
                spot = ticker.close
            if not spot or not math.isfinite(spot) or spot <= 0:
                logger.warning("Could not get SPX spot price for GEX computation")
                return None
            spot = float(spot)

            # Get option chains for SPX
            chains = ib.reqSecDefOptParams(spx.symbol, "", spx.secType, spx.conId)
            if not chains:
                logger.warning("No option chains returned for SPX")
                return None

            # Pick CBOE/SMART chain with nearest expirations
            chain = None
            for c in chains:
                if c.exchange in ("CBOE", "SMART"):
                    chain = c
                    break
            if chain is None:
                chain = chains[0]

            # Filter strikes within 5% of spot
            strike_min = spot * (1 - STRIKE_RANGE_PCT)
            strike_max = spot * (1 + STRIKE_RANGE_PCT)
            valid_strikes = sorted(
                s for s in chain.strikes if strike_min <= s <= strike_max
            )

            if not valid_strikes:
                logger.warning("No SPX strikes within %.0f%% of spot %.2f",
                               STRIKE_RANGE_PCT * 100, spot)
                return None

            # Pick nearest expirations
            sorted_expirations = sorted(chain.expirations)[:NEAREST_EXPIRATIONS]

            # Build option contracts for all strike/expiry combos
            contracts = []
            for exp in sorted_expirations:
                for strike in valid_strikes:
                    for right in ("C", "P"):
                        opt = Option("SPX", exp, strike, right, "CBOE")
                        contracts.append(opt)

            # Qualify in batch
            qualified = ib.qualifyContracts(*contracts)

            if not qualified:
                logger.warning("Could not qualify any SPX option contracts")
                return None

            # Request market data for all qualified contracts
            tickers = ib.reqTickers(*qualified)

            total_call_gex = 0.0
            total_put_gex = 0.0
            strikes_analyzed = 0
            all_strike_data = []
            strike_gex_map: Dict[float, float] = {}

            for t in tickers:
                contract = t.contract
                gamma = None
                oi = None

                # Get gamma from model greeks or last greeks
                greeks = t.modelGreeks or t.lastGreeks
                if greeks:
                    gamma = greeks.gamma
                    oi = greeks.undPrice  # Not OI — we need OI from summary

                # Try to get open interest from the ticker summary
                if hasattr(t, 'openInterest') and t.openInterest:
                    oi_val = float(t.openInterest)
                else:
                    # Fallback: use volume as proxy if OI not available
                    oi_val = float(t.volume) if t.volume and t.volume > 0 else 0.0

                if gamma is None or not math.isfinite(gamma) or oi_val <= 0:
                    continue

                # Naive GEX: gamma * OI * 100 * spot^2 * 0.01
                strike_gex = float(gamma) * oi_val * 100.0 * (spot ** 2) * 0.01
                strike_price = float(contract.strike)
                strikes_analyzed += 1

                if contract.right == "C":
                    total_call_gex += strike_gex
                else:  # Put
                    total_put_gex += strike_gex

                # Accumulate per-strike for wall/flip detection
                if strike_price not in strike_gex_map:
                    strike_gex_map[strike_price] = 0.0
                if contract.right == "C":
                    strike_gex_map[strike_price] += strike_gex
                else:
                    strike_gex_map[strike_price] -= strike_gex

            # Net GEX = sum(call GEX) - sum(put GEX)
            net_gex = total_call_gex - total_put_gex

            if not math.isfinite(net_gex):
                logger.warning("IBKR GEX computation resulted in NaN/Inf")
                return None

            for strike_price, net in sorted(strike_gex_map.items()):
                all_strike_data.append({"strike": strike_price, "net": net})

            display = _format_gex_display(net_gex)
            regime = self.classify_regime(net_gex)
            walls = self.find_walls(all_strike_data, spot)
            gamma_flip = self.find_gamma_flip(all_strike_data, spot)

            logger.info(
                "IBKR GEX computed: SPX spot=%.2f, net_gex=%s, regime=%s, "
                "strikes=%d, expirations=%d",
                spot, display, regime, strikes_analyzed, len(sorted_expirations),
            )

            return {
                "ticker": "SPX",
                "spot_price": round(spot, 2),
                "net_gex": net_gex,
                "net_gex_display": display,
                "regime": regime,
                "gamma_flip_strike": gamma_flip,
                "nearest_call_wall": walls.get("call_wall"),
                "nearest_put_wall": walls.get("put_wall"),
                "expirations_included": len(sorted_expirations),
                "strikes_analyzed": strikes_analyzed,
                "source": "ibkr",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.warning("IBKR GEX computation failed: %s", e)
            return None

    # ── Quant Data API (fallback) ───────────────────────────────

    def fetch_gex_data(self, ticker: str = "SPY") -> Optional[dict]:
        """
        Fetch raw GEX data from Quant Data API.
        Returns mock data if not enabled. Returns None on error.
        """
        if not self._enabled:
            return self._generate_mock_data(ticker)

        try:
            import requests
            url = f"{QUANTDATA_API_BASE}{QUANTDATA_ENDPOINTS['heat_map']}"
            params = {"ticker": ticker}
            resp = requests.get(
                url,
                headers=self._build_headers(),
                params=params,
                timeout=10,
            )

            if resp.status_code in (401, 403, 404):
                logger.warning(
                    "GEX API failed (HTTP %d) for %s — returning None",
                    resp.status_code, ticker,
                )
                return None

            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            logger.warning("GEX fetch failed for %s: %s", ticker, e)
            return None

    def compute_net_gex(self, raw_data: Optional[dict], ticker: str = "SPY") -> Optional[dict]:
        """
        Compute net GEX from raw API response.
        Returns structured dict with regime, flip, walls, etc.
        """
        if raw_data is None:
            return None

        try:
            # Handle mock data format
            if raw_data.get("_mock"):
                return raw_data

            # Parse real API response
            spot_cents = raw_data.get("stockPriceInCents", 0)
            spot_price = spot_cents / 100.0 if spot_cents else 0.0

            ticker_data = raw_data.get("tickerToHeatMapData", {})
            heat_map = ticker_data.get(ticker, {}).get("heatMap", [])

            if not heat_map or spot_price == 0:
                return None

            # Filter to nearest expirations
            sorted_exps = sorted(heat_map, key=lambda x: x.get("expirationDate", ""))
            nearest = sorted_exps[:NEAREST_EXPIRATIONS]

            total_net_gex = 0.0
            strikes_analyzed = 0
            strike_min = spot_price * (1 - STRIKE_RANGE_PCT)
            strike_max = spot_price * (1 + STRIKE_RANGE_PCT)

            all_strike_data = []

            for exp in nearest:
                strikes = exp.get("strikes", [])
                for strike_entry in strikes:
                    strike_price = strike_entry.get("strikePrice", 0) / 100.0
                    if strike_min <= strike_price <= strike_max:
                        call_val = strike_entry.get("callValue", 0)
                        put_val = strike_entry.get("putValue", 0)
                        net_strike = call_val + put_val
                        total_net_gex += net_strike
                        strikes_analyzed += 1
                        all_strike_data.append({
                            "strike": strike_price,
                            "net": net_strike,
                        })

            if not math.isfinite(total_net_gex):
                logger.warning("GEX total_net_gex is NaN/Inf for %s — returning None", ticker)
                return None

            display = _format_gex_display(total_net_gex)
            regime = self.classify_regime(total_net_gex)
            walls = self.find_walls(all_strike_data, spot_price)
            gamma_flip = self.find_gamma_flip(all_strike_data, spot_price)

            return {
                "ticker": ticker,
                "spot_price": round(spot_price, 2),
                "net_gex": total_net_gex,
                "net_gex_display": display,
                "regime": regime,
                "gamma_flip_strike": gamma_flip,
                "nearest_call_wall": walls.get("call_wall"),
                "nearest_put_wall": walls.get("put_wall"),
                "expirations_included": len(nearest),
                "strikes_analyzed": strikes_analyzed,
                "source": "quantdata",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.warning("GEX compute failed for %s: %s", ticker, e)
            return None

    @staticmethod
    def classify_regime(net_gex: float) -> str:
        """Classify GEX regime based on net gamma exposure."""
        if not math.isfinite(net_gex):
            logger.warning("GEX net_gex is NaN/Inf — defaulting to UNKNOWN")
            return "UNKNOWN"
        if net_gex > 5e9:
            return "STRONG_POSITIVE"
        elif net_gex > 0:
            return "MILD_POSITIVE"
        elif net_gex > -10e9:
            return "MILD_NEGATIVE"
        elif net_gex > -25e9:
            return "STRONG_NEGATIVE"
        else:
            return "EXTREME_NEGATIVE"

    def get_modifier_value(self) -> dict:
        """
        Return the current GEX-based position modifier.
        Safe default: 1.0 if data unavailable.
        """
        if not self._last_result:
            return {"value": 1.0, "reason": "GEX data unavailable"}

        # Use SPX (IBKR) if available, else QQQ/SPY from Quant Data
        result = (
            self._last_result.get("spx")
            or self._last_result.get("qqq")
            or self._last_result.get("spy")
        )
        if not result:
            return {"value": 1.0, "reason": "GEX data unavailable"}

        regime = result.get("regime", "UNKNOWN")
        display = result.get("net_gex_display", "N/A")
        mod = REGIME_MODIFIERS.get(regime, REGIME_MODIFIERS["UNKNOWN"])

        return {
            "value": mod["value"],
            "reason": mod["template"].format(display=display),
        }

    @staticmethod
    def find_gamma_flip(strike_data: list, spot_price: float) -> Optional[float]:
        """
        Find the gamma flip point: where cumulative net GEX crosses from
        negative to positive as we walk from below spot to above spot.
        """
        if not strike_data:
            return None

        sorted_strikes = sorted(strike_data, key=lambda x: x["strike"])
        cumulative = 0.0
        prev_cumulative = 0.0

        for entry in sorted_strikes:
            prev_cumulative = cumulative
            cumulative += entry["net"]
            if prev_cumulative < 0 and cumulative >= 0:
                return entry["strike"]

        return None

    @staticmethod
    def find_walls(strike_data: list, spot_price: float) -> dict:
        """
        Find call wall (highest positive GEX above spot) and
        put wall (highest negative GEX below spot).
        """
        call_wall = None
        call_wall_val = 0.0
        put_wall = None
        put_wall_val = 0.0

        for entry in strike_data:
            strike = entry["strike"]
            net = entry["net"]

            if strike > spot_price and net > call_wall_val:
                call_wall = strike
                call_wall_val = net

            if strike < spot_price and net < put_wall_val:
                put_wall = strike
                put_wall_val = net

        return {"call_wall": call_wall, "put_wall": put_wall}

    def update(self, urgency: str = "idle") -> Optional[dict]:
        """
        Fetch fresh GEX data if refresh interval has elapsed.

        Priority:
          1. IBKR options chain (SPX) — primary
          2. Quant Data API (SPY/QQQ) — fallback
          3. Mock data — dry-run

        Returns cached result if too soon. Caches and logs result.

        Args:
            urgency: "idle" (5min), "active" (2min), or "preflight" (1min)
        """
        interval = _URGENCY_INTERVALS.get(urgency, REFRESH_INTERVAL_IDLE)
        now = time.time()
        if (
            self._last_update is not None
            and (now - self._last_update) < interval
            and self._last_result is not None
        ):
            return self._last_result

        results = {}

        # Primary: try IBKR options chain for SPX
        ibkr_result = self.fetch_gex_from_ibkr()
        if ibkr_result:
            results["spx"] = ibkr_result
        else:
            # Fallback: Quant Data API for SPY/QQQ
            for ticker in TICKERS:
                raw = self.fetch_gex_data(ticker)
                computed = self.compute_net_gex(raw, ticker)
                if computed:
                    results[ticker.lower()] = computed

        if results:
            self._last_result = results
            self._last_update = now
            self._log_gamma_levels(results)

        return self._last_result

    def get_cached(self) -> Optional[dict]:
        """Return last result without fetching."""
        return self._last_result

    def _log_gamma_levels(self, results: dict) -> None:
        """Append GEX results to logs/gamma_levels.json."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **results,
        }
        try:
            with open(self._gamma_log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning("Failed to write gamma_levels.json: %s", e)

    # ── Mock data for dry-run mode ──────────────────────────────

    def _generate_mock_data(self, ticker: str) -> dict:
        """
        Generate realistic mock GEX data for dry-run testing.
        Simulates negative gamma regime matching current market conditions.
        Net GEX cycles between -8B and -18B.
        """
        self._mock_cycle += 1
        cycle_pos = math.sin(self._mock_cycle * 0.3)

        # Cycle net_gex between -8B and -18B
        net_gex = -13e9 + cycle_pos * 5e9  # range: -18B to -8B

        # Spot prices (approximate)
        spots = {"SPY": 583.55, "QQQ": 502.30}
        spot = spots.get(ticker, 500.0) + random.uniform(-2, 2)

        display = _format_gex_display(net_gex)
        regime = self.classify_regime(net_gex)

        # Generate mock strike data for flip/wall calculation
        gamma_flip = round(spot - 3.0 + random.uniform(-1, 1), 2)
        call_wall = round(spot + 5.0 + random.uniform(-1, 1), 2)
        put_wall = round(spot - 8.0 + random.uniform(-1, 1), 2)

        return {
            "_mock": True,
            "ticker": ticker,
            "spot_price": round(spot, 2),
            "net_gex": net_gex,
            "net_gex_display": display,
            "regime": regime,
            "gamma_flip_strike": gamma_flip,
            "nearest_call_wall": call_wall,
            "nearest_put_wall": put_wall,
            "expirations_included": 4,
            "strikes_analyzed": 42,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
