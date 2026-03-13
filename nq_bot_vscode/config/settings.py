"""
NQ Trading Bot -- Master Configuration v1.3.1
=============================================
CONFIGURED FOR:
- Broker: Tradovate (paper -> live)
- Instrument: MNQ (Micro Nasdaq-100)
- Strategy: 5-contract scale-out with delayed C3 runner
- Account: $50,000
- Validated: 396 trades, PF 2.86, +$47,236, 1.60% max DD
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
import os


class MarketRegime(Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_LIQUIDITY = "low_liquidity"
    EVENT_DRIVEN = "event_driven"
    CRASH = "crash"
    UNKNOWN = "unknown"


class TradeDirection(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalConfidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOISE = "noise"


@dataclass
class DatabaseConfig:
    host: str = os.getenv("PG_HOST", "localhost")
    port: int = int(os.getenv("PG_PORT", "5432"))
    database: str = os.getenv("PG_DATABASE", "nq_trading")
    user: str = os.getenv("PG_USER", "nq_bot")
    password: str = os.getenv("PG_PASSWORD", "")

    @property
    def connection_params(self) -> dict:
        """Return individual connection parameters for asyncpg.

        Use with: asyncpg.create_pool(**config.db.connection_params)
        This avoids assembling the password into a URI string that
        could be logged or displayed.
        """
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": self.password,
        }

    @property
    def dsn(self) -> str:
        """Return a masked DSN safe for logging (password redacted)."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"


@dataclass
class DiscordConfig:
    """MikesTrades server -> #alerts channel."""
    token: str = os.getenv("DISCORD_TOKEN", "")
    channel_ids: list = field(default_factory=lambda: 
        [x.strip() for x in os.getenv("DISCORD_CHANNEL_IDS", "").split(",") if x.strip()]
    )
    server_name: str = "MikesTrades"
    channel_name: str = "alerts"
    
    bullish_keywords: list = field(default_factory=lambda: [
        "long", "buy", "bullish", "calls", "longs", "bid",
        "green", "rip", "bounce", "support holding", "breakout",
        "higher", "buyers stepping in", "demand zone",
        "long nq", "long mnq", "long nas", "buy dip", "look for longs",
        "going long", "took longs", "targeting", "break above",
        "reclaim", "held support", "strong bid",
    ])
    bearish_keywords: list = field(default_factory=lambda: [
        "short", "sell", "bearish", "puts", "shorts", "offer",
        "dump", "red", "fade", "rejection", "resistance", "breakdown",
        "lower", "sellers", "supply zone",
        "short nq", "short mnq", "short nas", "look for shorts",
        "going short", "took shorts", "break below",
        "failed to hold", "lost support", "heavy offers",
    ])
    min_bias_confidence: float = 0.6
    signal_cooldown_seconds: int = 120


@dataclass
class TradovateConfig:
    """
    Tradovate REST + WebSocket API.
    Docs: https://api.tradovate.com/
    """
    username: str = os.getenv("TRADOVATE_USERNAME", "")
    password: str = os.getenv("TRADOVATE_PASSWORD", "")
    app_id: str = os.getenv("TRADOVATE_APP_ID", "")
    app_version: str = "1.0"
    cid: int = int(os.getenv("TRADOVATE_CID", "0"))
    sec: str = os.getenv("TRADOVATE_SECRET", "")
    device_id: str = os.getenv("TRADOVATE_DEVICE_ID", "nq-bot-01")
    environment: str = os.getenv("TRADOVATE_ENV", "demo")
    
    @property
    def base_url(self) -> str:
        return "https://live.tradovate.com/v1" if self.environment == "live" else "https://demo.tradovate.com/v1"
    
    @property
    def md_ws_url(self) -> str:
        return "wss://md.tradovate.com/v1/websocket"
    
    @property
    def order_ws_url(self) -> str:
        return "wss://live.tradovate.com/v1/websocket" if self.environment == "live" else "wss://demo.tradovate.com/v1/websocket"
    
    # Front-month MNQ -- resolved dynamically at startup via ContractRoller.
    # Fallback value used only if ContractRoller import fails.
    symbol: str = "MNQM6"

    @staticmethod
    def resolve_front_month(base: str = "MNQ") -> str:
        """Resolve current front-month symbol using ContractRoller."""
        try:
            from Broker.contract_roller import ContractRoller
            return ContractRoller.get_front_month(base)
        except Exception:
            return "MNQM6"  # Fallback
    
    replay_period_days: int = 30
    max_requests_per_second: int = 5
    reconnect_delay_seconds: int = 5
    max_reconnect_attempts: int = 10


@dataclass
class ScaleOutConfig:
    """
    4-Contract Scale-Out v3 -- Delayed C3 Runner Architecture.

    C1 (1): 5-bar time exit -- the "canary" that validates direction.
    C2 (1): Structural target -- exits at nearest swing point.
    C3 (2): ATR trailing runner -- DELAYED ENTRY (only stays open when
            C1 exits profitably. If C1 loses, C3 closed immediately).

    Win architecture:
      Best:  C1 wins, C3 trails big move           -> $20 + $800+  = $820+
      Good:  C1 wins, C2/C3 at breakeven            -> $20 + $0     = $20
      Ok:    C1 loses, C3 blocked, C2 at stop        -> -$80 (2 contracts only)
      Worst: All hit initial stop (Phase 1)          -> -$200 (5 contracts)
    """
    total_contracts: int = 5              # Max contracts: C1=1, C2=1, C3=3

    # Contract 1 -- The Scalp (B:5 bars time exit, PF 1.81 validated)
    c1_contracts: int = 1
    c1_time_exit_bars: int = 5             # Exit C1 at market after N bars if profitable
    c1_max_bars_fallback: int = 12         # Fallback: exit at market if still profitable after N bars

    # Legacy: Trail-from-profit params (archived, use for A/B testing only)
    c1_profit_threshold_pts: float = 3.0   # Archived: trailing activation threshold
    c1_trail_distance_pts: float = 2.5     # Archived: trail distance from HWM

    # Contract 2 -- The Medium (15-bar time exit, captures follow-through)
    c2_contracts: int = 1
    c2_time_exit_bars: int = 15            # Exit C2 at market after N bars post-C1 exit
    c2_move_stop_to_breakeven: bool = True
    c2_breakeven_buffer_points: float = 2.0   # Entry + 2pts = avoids stop-hunting at exact entry (Osler 2005)
    c2_trailing_stop_enabled: bool = True
    c2_trailing_stop_type: str = "atr"        # "atr", "fixed", "swing"
    c2_trailing_atr_multiplier: float = 2.0
    c2_trailing_fixed_points: float = 30.0
    c2_max_target_points: float = 150.0
    c2_time_stop_minutes: int = 120

    # C2 Breakeven Variant (optimized Feb 2026 -- run scripts/c2_be_optimizer.py to validate)
    # "A" = No BE: C2 keeps initial stop; ATR trail provides sole protection
    # "B" = Delayed: BE moves only after C2 MFE >= c2_be_delay_multiplier × stop_distance
    # "C" = Partial: BE at midpoint between initial stop and entry (entry - stop/2)
    # "D" = Current/immediate: BE at entry+1 the instant C1 exits (original behavior)
    c2_be_variant: str = "B"                  # Default: delayed BE to prevent stolen runners
    c2_be_delay_multiplier: float = 1.5       # Variant B: MFE threshold = stop_distance × this (raised back: give runner more room before BE)

    # ── C3 Runner (THE KEY EDGE) ─────────────────────────────────
    # C3 only stays open when C1 exits profitably.
    # If C1 loses → C3 is closed immediately at market.
    # Motto: "Let winners win BIG" — C3 is the profit multiplier.
    c3_contracts: int = 3                     # v1.3.1: 3 runner contracts (PF 2.86)
    c3_delayed_entry_enabled: bool = True
    c3_trailing_atr_multiplier: float = 3.0   # Wider trail than C2 — runner needs room to breathe
    c3_max_target_points: float = 300.0       # 2x C2's cap — let fat-tail moves pay out
    c3_time_stop_minutes: int = 240           # 4 hours — full session runway for trend days

    # Adaptive Exit Configuration (regime-aware parameters)
    # Requires walk-forward validation before enabling in production.
    # Research: Kaminski & Lo (2014), Nystrup et al. (2017) -- 2-param adaptation optimal
    adaptive_exits_enabled: bool = False      # Enable regime-adaptive BE + trail (requires walk-forward validation)


@dataclass
class RiskConfig:
    """Tuned for $50K, 2x MNQ."""
    account_size: float = 50_000.0
    max_risk_per_trade_pct: float = 0.8      # $400 max risk per trade (tighter per-trade cap)
    max_daily_loss_pct: float = 3.0          # $1,500 daily limit (room for multiple trades)
    max_weekly_loss_pct: float = 5.0
    max_total_drawdown_pct: float = 10.0     # $5,000 = kill switch
    
    max_contracts_micro: int = 5              # v1.3.1: C1=1 + C2=1 + C3=3
    max_contracts_mini: int = 0
    use_micro: bool = True
    
    nq_tick_value_mini: float = 5.0
    nq_tick_value_micro: float = 0.50
    nq_point_value_mini: float = 20.0
    nq_point_value_micro: float = 2.0
    
    max_slippage_ticks: int = 4
    commission_per_contract: float = 1.29    # Tradovate MNQ commission
    
    atr_period: int = 14
    atr_multiplier_stop: float = 2.0
    atr_multiplier_target: float = 1.5
    min_rr_ratio: float = 1.5
    
    max_vix_for_full_size: float = 25.0
    max_vix_for_trading: float = 40.0
    min_volume_threshold: int = 5000
    
    no_trade_minutes_before_news: int = 15
    no_trade_minutes_after_news: int = 10
    reduce_size_overnight: bool = True
    overnight_start_hour: int = 18
    overnight_end_hour: int = 8
    
    kill_switch_enabled: bool = True
    kill_switch_max_consecutive_losses: int = 5
    kill_switch_cooldown_minutes: int = 60

    def get_point_value(self, instrument: str = "MNQ") -> float:
        """Get point value for an instrument, falling back to MNQ defaults."""
        try:
            from config.instruments import InstrumentSpec
            return InstrumentSpec.from_symbol(instrument).point_value
        except Exception:
            return self.nq_point_value_micro if self.use_micro else self.nq_point_value_mini

    def get_tick_size(self, instrument: str = "MNQ") -> float:
        """Get tick size for an instrument, falling back to MNQ defaults."""
        try:
            from config.instruments import InstrumentSpec
            return InstrumentSpec.from_symbol(instrument).tick_size
        except Exception:
            return 0.25

    def get_commission(self, instrument: str = "MNQ") -> float:
        """Get commission per contract for an instrument."""
        try:
            from config.instruments import InstrumentSpec
            return InstrumentSpec.from_symbol(instrument).commission_per_contract
        except Exception:
            return self.commission_per_contract


@dataclass
class FeatureConfig:
    ob_lookback_bars: int = 50
    ob_min_displacement_atr: float = 1.5
    ob_max_age_bars: int = 200
    fvg_min_gap_ticks: int = 8
    fvg_max_age_bars: int = 100
    fvg_fill_threshold_pct: float = 0.5
    ifvg_confirmation_bars: int = 3
    sweep_lookback_bars: int = 20
    sweep_min_wick_ratio: float = 0.6
    sweep_volume_spike_multiplier: float = 1.5
    vwap_deviation_bands: list = field(default_factory=lambda: [1.0, 2.0, 3.0])
    volume_profile_lookback_days: int = 5
    poc_proximity_ticks: int = 20
    delta_lookback_bars: int = 10
    cumulative_delta_divergence_threshold: float = 0.3


@dataclass
class ExecutionConfig:
    broker: str = "tradovate"
    paper_trading: bool = True
    order_timeout_seconds: int = 10
    max_retry_attempts: int = 3
    use_limit_orders: bool = True
    limit_offset_ticks: int = 1
    simulated_latency_ms: int = 50
    simulated_slippage_ticks: int = 2


@dataclass
class SignalConfig:
    min_confluence_score: float = 0.60
    discord_weight: float = 0.25
    technical_weight: float = 0.50
    ml_weight: float = 0.25
    min_signals_aligned: int = 3
    max_signals_required: int = 7


@dataclass
class AlertConfig:
    """Real-time alerting configuration."""
    enabled_channels: List[str] = field(default_factory=lambda: ["console"])
    discord_webhook_url: str = os.getenv("ALERT_DISCORD_WEBHOOK_URL", "")
    telegram_bot_token: str = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
    rate_limit_seconds: int = 300  # 5 min per event type (EMERGENCY bypasses)


@dataclass
class DataPipelineConfig:
    """
    Primary: Tradovate WebSocket live bars
    Fallback: TradingView CSV export for backtesting
    """
    primary_source: str = "tradovate"
    tv_export_directory: str = os.getenv("TV_EXPORT_DIR", "./data/tradingview")
    bar_size_minutes: int = 1
    higher_timeframes: list = field(default_factory=lambda: [5, 15, 60])
    keep_raw_ticks: bool = False
    bar_retention_days: int = 365
    vix_source: str = "tradovate"


@dataclass
class BotConfig:
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    tradovate: TradovateConfig = field(default_factory=TradovateConfig)
    scale_out: ScaleOutConfig = field(default_factory=ScaleOutConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    data_pipeline: DataPipelineConfig = field(default_factory=DataPipelineConfig)
    alerting: AlertConfig = field(default_factory=AlertConfig)
    log_level: str = "INFO"
    environment: str = "paper"
    heartbeat_interval_seconds: int = 5
    instrument: str = "MNQ"  # Supported: MNQ, MES, MYM, M2K

    @property
    def instrument_spec(self):
        """Get the InstrumentSpec for the configured instrument."""
        from config.instruments import InstrumentSpec
        return InstrumentSpec.from_symbol(self.instrument)

    @property
    def point_value(self) -> float:
        """Instrument point value (delegates to InstrumentSpec if available)."""
        try:
            from config.instruments import InstrumentSpec
            return InstrumentSpec.from_symbol(self.instrument).point_value
        except Exception:
            return self.risk.nq_point_value_micro

    @property
    def tick_size(self) -> float:
        """Instrument tick size."""
        try:
            from config.instruments import InstrumentSpec
            return InstrumentSpec.from_symbol(self.instrument).tick_size
        except Exception:
            return 0.25


CONFIG = BotConfig()
