"""
NQ Trading Bot — Master Configuration
======================================
CONFIGURED FOR:
- Broker: Tradovate (paper → live)
- Instrument: MNQ (Micro Nasdaq-100)
- Strategy: 2-contract scale-out
- Account: $50,000
- Discord: MikesTrades #alerts
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
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass
class DiscordConfig:
    """MikesTrades server → #alerts channel."""
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
    
    # Current front-month MNQ — UPDATE QUARTERLY
    # H=Mar, M=Jun, U=Sep, Z=Dec + 2-digit year
    symbol: str = "MNQM5"
    
    replay_period_days: int = 30
    max_requests_per_second: int = 5
    reconnect_delay_seconds: int = 5
    max_reconnect_attempts: int = 10


@dataclass
class ScaleOutConfig:
    """
    2-Contract Scale-Out — THE BREAD AND BUTTER.

    Contract 1: Trail-from-profit exit (Variant C)
      Once unrealized profit >= c1_profit_threshold_pts, activate a trailing
      stop c1_trail_distance_pts behind the high-water mark. If profit never
      reaches threshold within c1_max_bars_fallback bars, exit at market.
    Contract 2: Runner, stop to breakeven+1 after C1 exits, then trail

    Win-win architecture:
      Best:  C1 trails a big move + C2 runs big  → $40+ + $200 = $240+
      Good:  C1 trails small move + C2 at BE     → $10  + $2   = $12
      Worst: Both at initial stop                → Controlled loss (~$60-80)
    """
    total_contracts: int = 2

    # Contract 1 — Trail-from-profit (Variant C, validated Feb 2026)
    c1_contracts: int = 1
    c1_profit_threshold_pts: float = 3.0    # Activate trailing once profit >= this
    c1_trail_distance_pts: float = 2.5      # Trail distance from HWM
    c1_max_bars_fallback: int = 12          # Fallback market exit if trail never activates

    # Legacy: Time-based exit (archived, use for A/B testing only)
    c1_time_exit_bars: int = 10             # Old: exit C1 after N bars if profitable

    # Contract 2 — Runner
    c2_contracts: int = 1
    c2_move_stop_to_breakeven: bool = True
    c2_breakeven_buffer_points: float = 1.0   # Entry + 1pt = guaranteed small win
    c2_trailing_stop_enabled: bool = True
    c2_trailing_stop_type: str = "atr"        # "atr", "fixed", "swing"
    c2_trailing_atr_multiplier: float = 2.0
    c2_trailing_fixed_points: float = 30.0
    c2_max_target_points: float = 150.0
    c2_time_stop_minutes: int = 120
    
    c2_be_trigger: str = "c1_exited"           # Move stop to BE when C1 exits (time or stop)


@dataclass
class RiskConfig:
    """Tuned for $50K, 2x MNQ."""
    account_size: float = 50_000.0
    max_risk_per_trade_pct: float = 1.0      # $500 max risk per trade
    max_daily_loss_pct: float = 3.0          # $1,500 daily limit
    max_weekly_loss_pct: float = 5.0
    max_total_drawdown_pct: float = 10.0     # $5,000 = kill switch
    
    max_contracts_micro: int = 2
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
    log_level: str = "INFO"
    environment: str = "paper"
    heartbeat_interval_seconds: int = 5


CONFIG = BotConfig()
