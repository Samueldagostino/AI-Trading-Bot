"""
QuantData Client — Ingests options market context data.
========================================================
Supports two modes:
  - API mode: pulls from discovered endpoints automatically
  - Manual mode: reads from a user-populated JSON file

Both modes produce identical MarketContext objects.

STATUS: LOG-ONLY. MarketContext is recorded per-trade but does NOT
affect confluence scoring until validated via correlation analysis.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from data_feeds.market_context import MarketContext

logger = logging.getLogger(__name__)

# Paths relative to the nq_bot_vscode directory
_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _MODULE_DIR.parent
_CONFIG_DIR = _PROJECT_DIR / "config"


class QuantDataClient:
    """
    Dual-mode QuantData client.

    API mode: fetches from discovered endpoints (config/quantdata_endpoints.json).
    Manual mode: reads from user-populated JSON (config/quantdata_manual_input.json).

    Both modes produce identical MarketContext objects for downstream logging.
    """

    ENDPOINTS_PATH = _CONFIG_DIR / "quantdata_endpoints.json"
    MANUAL_INPUT_PATH = _CONFIG_DIR / "quantdata_manual_input.json"
    STALE_THRESHOLD = timedelta(hours=2)

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            self._endpoints_path = Path(config_path)
        else:
            self._endpoints_path = self.ENDPOINTS_PATH

        self.config = self._load_config()
        self.mode = self.config.get("status", "manual")
        self.last_snapshot: Optional[MarketContext] = None
        self.last_update: Optional[datetime] = None

    def _load_config(self) -> dict:
        """Load endpoint configuration."""
        if self._endpoints_path.exists():
            try:
                return json.loads(self._endpoints_path.read_text())
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to parse endpoints config: %s", e)
        return {"status": "manual"}

    # ──────────────────────────────────────────────────────────
    # API MODE (Path A) — discovered endpoints
    # ──────────────────────────────────────────────────────────

    async def fetch_gex(self, symbol: str = "SPY") -> dict:
        """Fetch gamma exposure by strike from QuantData API."""
        endpoint = self.config.get("endpoints", {}).get("gex")
        if not endpoint:
            logger.warning("No GEX endpoint configured — returning defaults")
            return self._default_gex()

        try:
            data = await self._api_get(endpoint, symbol)
            return {
                "total_gex": data.get("total_gex", 0),
                "gamma_regime": self._classify_gamma(data.get("total_gex", 0)),
                "gamma_flip_level": data.get("gamma_flip_level", 0),
                "nearest_wall_above": data.get("nearest_wall_above"),
                "nearest_wall_below": data.get("nearest_wall_below"),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error("GEX fetch failed: %s", e)
            return self._default_gex()

    async def fetch_net_flow(self, symbol: str = "SPY") -> dict:
        """Fetch net options premium flow."""
        endpoint = self.config.get("endpoints", {}).get("net_flow")
        if not endpoint:
            return self._default_flow()

        try:
            data = await self._api_get(endpoint, symbol)
            call_prem = data.get("call_premium", 0)
            put_prem = data.get("put_premium", 0)
            net = call_prem - put_prem
            return {
                "call_premium": call_prem,
                "put_premium": put_prem,
                "net_premium": net,
                "flow_direction": self._classify_flow(net),
                "underlying_price": data.get("underlying_price", 0),
            }
        except Exception as e:
            logger.error("Net flow fetch failed: %s", e)
            return self._default_flow()

    async def fetch_dark_pool(self, symbol: str = "SPY") -> dict:
        """Fetch dark pool / off-exchange institutional prints."""
        endpoint = self.config.get("endpoints", {}).get("dark_pool")
        if not endpoint:
            return self._default_dark_pool()

        try:
            data = await self._api_get(endpoint, symbol)
            return {
                "total_dark_volume": data.get("total_dark_volume", 0),
                "dark_bias": data.get("dark_bias", "neutral"),
                "largest_print": data.get("largest_print"),
                "dark_pool_levels": data.get("dark_pool_levels", []),
            }
        except Exception as e:
            logger.error("Dark pool fetch failed: %s", e)
            return self._default_dark_pool()

    async def fetch_vol_skew(self, symbol: str = "SPY") -> dict:
        """Fetch implied volatility skew across strikes."""
        endpoint = self.config.get("endpoints", {}).get("vol_skew")
        if not endpoint:
            return self._default_vol_skew()

        try:
            data = await self._api_get(endpoint, symbol)
            put_skew = data.get("put_skew", 0)
            call_skew = data.get("call_skew", 0)
            slope = put_skew - call_skew
            return {
                "put_skew": put_skew,
                "call_skew": call_skew,
                "skew_slope": slope,
                "skew_regime": self._classify_skew(slope),
            }
        except Exception as e:
            logger.error("Vol skew fetch failed: %s", e)
            return self._default_vol_skew()

    async def _api_get(self, endpoint: str, symbol: str) -> dict:
        """Make an authenticated API request."""
        import aiohttp

        base_url = self.config.get("base_url", "")
        auth_method = self.config.get("auth_method", "")
        auth_value = os.environ.get("QUANTDATA_AUTH", self.config.get("auth_value", ""))

        url = f"{base_url}{endpoint}"
        if "{symbol}" in url:
            url = url.replace("{symbol}", symbol)
        elif "symbol=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}symbol={symbol}"

        headers = {}
        if auth_method == "bearer":
            headers["Authorization"] = f"Bearer {auth_value}"
        elif auth_method == "api_key":
            headers["X-API-Key"] = auth_value
        elif auth_method == "cookie":
            headers["Cookie"] = auth_value

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _fetch_all_api(self, symbol: str = "SPY") -> dict:
        """Fetch all data points from API."""
        gex = await self.fetch_gex(symbol)
        flow = await self.fetch_net_flow(symbol)
        dark = await self.fetch_dark_pool(symbol)
        skew = await self.fetch_vol_skew(symbol)

        return {
            "timestamp": datetime.now(),
            "gamma_regime": gex.get("gamma_regime", "neutral"),
            "total_gex": gex.get("total_gex", 0),
            "gamma_flip_level": gex.get("gamma_flip_level", 0),
            "nearest_wall_above": gex.get("nearest_wall_above"),
            "nearest_wall_below": gex.get("nearest_wall_below"),
            "flow_direction": flow.get("flow_direction", "neutral"),
            "net_premium": flow.get("net_premium", 0),
            "call_premium": flow.get("call_premium", 0),
            "put_premium": flow.get("put_premium", 0),
            "dark_bias": dark.get("dark_bias", "neutral"),
            "dark_pool_levels": dark.get("dark_pool_levels"),
            "skew_regime": skew.get("skew_regime", "normal"),
            "skew_slope": skew.get("skew_slope", 0),
            "source": "api",
            "age_seconds": 0.0,
        }

    # ──────────────────────────────────────────────────────────
    # MANUAL MODE (Path B) — user-populated JSON
    # ──────────────────────────────────────────────────────────

    def load_manual_snapshot(self) -> dict:
        """
        Read from config/quantdata_manual_input.json.
        User populates this file from the QuantData dashboard.
        """
        path = self.MANUAL_INPUT_PATH
        if not path.exists():
            logger.info("No manual input file at %s — using neutral defaults", path)
            return self._default_neutral_context()

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse manual input: %s", e)
            return self._default_neutral_context()

        ts_str = data.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            age = (datetime.now() - ts).total_seconds()
        except (ValueError, TypeError):
            ts = datetime.now()
            age = 0.0

        if age > self.STALE_THRESHOLD.total_seconds():
            logger.warning(
                "QuantData manual snapshot is %.1f hours old — may be stale",
                age / 3600,
            )

        gex = data.get("gex", {})
        flow = data.get("net_flow", {})
        dark = data.get("dark_pool", {})
        skew = data.get("vol_skew", {})

        return {
            "timestamp": ts,
            "gamma_regime": gex.get("gamma_regime", "neutral"),
            "total_gex": gex.get("total_gex_millions", 0),
            "gamma_flip_level": gex.get("gamma_flip_level", 0),
            "nearest_wall_above": gex.get("nearest_wall_above"),
            "nearest_wall_below": gex.get("nearest_wall_below"),
            "flow_direction": flow.get("flow_direction", "neutral"),
            "net_premium": (
                (flow.get("call_premium_billions", 0) - flow.get("put_premium_billions", 0))
                * 1_000_000_000
            ),
            "call_premium": flow.get("call_premium_billions", 0) * 1_000_000_000,
            "put_premium": flow.get("put_premium_billions", 0) * 1_000_000_000,
            "dark_bias": dark.get("dark_bias", "neutral"),
            "dark_pool_levels": dark.get("significant_levels"),
            "skew_regime": skew.get("skew_regime", "normal"),
            "skew_slope": skew.get("skew_slope", 0),
            "source": "manual",
            "age_seconds": age,
        }

    # ──────────────────────────────────────────────────────────
    # SHARED — both modes produce MarketContext
    # ──────────────────────────────────────────────────────────

    async def get_market_context(self, symbol: str = "SPY") -> MarketContext:
        """
        Returns a frozen MarketContext snapshot.
        Uses API mode if available, falls back to manual.
        """
        if self.mode == "discovered":
            try:
                data = await self._fetch_all_api(symbol)
            except Exception as e:
                logger.error("API fetch failed, falling back to manual: %s", e)
                data = self.load_manual_snapshot()
        else:
            data = self.load_manual_snapshot()

        # Ensure timestamp is a datetime
        ts = data.get("timestamp", datetime.now())
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                ts = datetime.now()

        context = MarketContext(
            timestamp=ts,
            gamma_regime=data.get("gamma_regime", "neutral"),
            total_gex=float(data.get("total_gex", 0)),
            gamma_flip_level=float(data.get("gamma_flip_level", 0)),
            nearest_wall_above=data.get("nearest_wall_above"),
            nearest_wall_below=data.get("nearest_wall_below"),
            flow_direction=data.get("flow_direction", "neutral"),
            net_premium=float(data.get("net_premium", 0)),
            call_premium=float(data.get("call_premium", 0)),
            put_premium=float(data.get("put_premium", 0)),
            dark_bias=data.get("dark_bias", "neutral"),
            dark_pool_levels=data.get("dark_pool_levels"),
            skew_regime=data.get("skew_regime", "normal"),
            skew_slope=float(data.get("skew_slope", 0)),
            source=data.get("source", "manual"),
            age_seconds=float(data.get("age_seconds", 0)),
        )

        self.last_snapshot = context
        self.last_update = datetime.now()

        logger.info(
            "MarketContext refreshed: gamma=%s, flow=%s, dark=%s, skew=%s [source=%s, age=%.0fs]",
            context.gamma_regime,
            context.flow_direction,
            context.dark_bias,
            context.skew_regime,
            context.source,
            context.age_seconds,
        )

        return context

    # ──────────────────────────────────────────────────────────
    # DEFAULTS — neutral context when no data available
    # ──────────────────────────────────────────────────────────

    def _default_neutral_context(self) -> dict:
        """Return all-neutral defaults when no data is available."""
        return {
            "timestamp": datetime.now(),
            "gamma_regime": "neutral",
            "total_gex": 0,
            "gamma_flip_level": 0,
            "nearest_wall_above": None,
            "nearest_wall_below": None,
            "flow_direction": "neutral",
            "net_premium": 0,
            "call_premium": 0,
            "put_premium": 0,
            "dark_bias": "neutral",
            "dark_pool_levels": None,
            "skew_regime": "normal",
            "skew_slope": 0,
            "source": "default",
            "age_seconds": 0,
        }

    def _default_gex(self) -> dict:
        return {
            "total_gex": 0, "gamma_regime": "neutral",
            "gamma_flip_level": 0,
            "nearest_wall_above": None, "nearest_wall_below": None,
        }

    def _default_flow(self) -> dict:
        return {
            "call_premium": 0, "put_premium": 0,
            "net_premium": 0, "flow_direction": "neutral",
        }

    def _default_dark_pool(self) -> dict:
        return {
            "total_dark_volume": 0, "dark_bias": "neutral",
            "largest_print": None, "dark_pool_levels": [],
        }

    def _default_vol_skew(self) -> dict:
        return {
            "put_skew": 0, "call_skew": 0,
            "skew_slope": 0, "skew_regime": "normal",
        }

    # ──────────────────────────────────────────────────────────
    # CLASSIFIERS — raw values → regime labels
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _classify_gamma(total_gex: float) -> str:
        """Classify gamma regime from total GEX value."""
        if total_gex > 100_000_000:
            return "positive"
        elif total_gex < -100_000_000:
            return "negative"
        return "neutral"

    @staticmethod
    def _classify_flow(net_premium: float) -> str:
        """Classify flow direction from net premium."""
        if net_premium > 50_000_000:
            return "bullish"
        elif net_premium < -50_000_000:
            return "bearish"
        return "neutral"

    @staticmethod
    def _classify_skew(skew_slope: float) -> str:
        """Classify volatility skew regime."""
        if skew_slope > 0.25:
            return "extreme"
        elif skew_slope > 0.12:
            return "elevated"
        return "normal"
