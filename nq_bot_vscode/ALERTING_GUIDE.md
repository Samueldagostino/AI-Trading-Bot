# Real-Time Alerting System Guide

## Overview

The alerting system is an institutional-grade async notification module that sends real-time alerts for critical trading events, risk conditions, and system status changes.

**Key Features:**
- Multiple notification channels (Console, Discord, Telegram)
- Rate limiting (max 1 alert per event type per 5 min, except EMERGENCY)
- Non-blocking async queue (never blocks trading loop)
- Exponential backoff retry (3 attempts per channel)
- Rich message formatting (Discord embeds, Telegram HTML)
- Pre-built alert templates for all event types

---

## Quick Start

### 1. Configure Environment

Copy the example environment file and add your credentials:

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
# Enable desired channels
ALERT_CHANNELS=console,discord,telegram

# Discord webhook (get from Server → Settings → Webhooks)
DISCORD_WEBHOOK_URL=https://discordapp.com/api/webhooks/YOUR_ID/YOUR_TOKEN

# Telegram Bot (create via @BotFather)
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789

# Rate limiting (seconds between alerts of same type)
ALERT_RATE_LIMIT_SECONDS=300
```

### 2. Initialize in Main Orchestrator

In your `TradingOrchestrator.__init__()`:

```python
from monitoring.alerting import AlertManager, set_alert_manager
from config.settings import CONFIG

class TradingOrchestrator:
    def __init__(self, config: BotConfig = CONFIG):
        # ... existing code ...

        # Initialize alerting
        self.alert_manager = AlertManager(
            config.alerting,
            rate_limit_seconds=config.alerting.rate_limit_seconds,
        )
        set_alert_manager(self.alert_manager)  # Set singleton
```

### 3. Start/Stop Alert Worker

In your async main loop:

```python
async def run(self):
    # Start alert worker at beginning
    await self.alert_manager.start()

    try:
        # Your trading loop here
        while self.running:
            # ... trading code ...
            pass
    finally:
        # Stop at shutdown
        await self.alert_manager.stop()
```

### 4. Send Alerts from Trading Loop

```python
from monitoring.alert_templates import AlertTemplates

# Trade entry
alert = AlertTemplates.trade_entry(
    direction="LONG",
    contracts=2,
    entry_price=20000.0,
    stop_loss=19990.0,
    take_profit=20020.0,
    signal_confidence=0.85,
)
self.alert_manager.enqueue(alert)  # Non-blocking!

# Kill switch
alert = AlertTemplates.kill_switch_triggered(
    reason="Max drawdown exceeded",
    stats={"drawdown_pct": 3.2, "daily_pnl": -1600},
)
self.alert_manager.enqueue(alert)
```

---

## Alert Types & Templates

### CRITICAL EVENTS (EMERGENCY severity)

#### Kill Switch Triggered
```python
alert = AlertTemplates.kill_switch_triggered(
    reason="Max consecutive losses: 5",
    stats={
        "consecutive_losses": 5,
        "max_consecutive_losses": 5,
        "equity": 48500.0,
    }
)
```

**Characteristics:**
- Severity: EMERGENCY (bypasses rate limit)
- Event type: `kill_switch_triggered`
- Action: Always sent immediately, no rate limiting

#### Connection Loss
```python
alert = AlertTemplates.connection_loss(
    component="tradovate_market_data",
    error="WebSocket disconnected, code 1006",
)
```

**Characteristics:**
- Severity: CRITICAL
- Event type: `connection_loss`
- Channels: All (console + Discord + Telegram)

#### System Error
```python
alert = AlertTemplates.system_error(
    component="execution_engine",
    error="Order submission failed: ECONNREFUSED",
)
```

**Characteristics:**
- Severity: CRITICAL
- Event type: `system_error`

### RISK WARNINGS (WARNING severity)

#### Drawdown Warning
```python
alert = AlertTemplates.drawdown_warning(
    current_drawdown_pct=2.5,
    max_drawdown_pct=3.0,
    daily_pnl=-1250.0,
)
```

**Characteristics:**
- Severity: WARNING
- Event type: `drawdown_warning`
- Rate limited: Yes (1 per 5 min)
- When to send: When drawdown > 2% and approaching 3% limit

#### Consecutive Loss Streak
```python
alert = AlertTemplates.consecutive_loss_streak(
    consecutive_losses=3,
    avg_loss=-75.0,
    total_loss=-225.0,
)
```

**Characteristics:**
- Severity: WARNING
- Event type: `consecutive_losses`
- Rate limited: Yes
- When to send: After 3+ consecutive losses

#### High VIX Alert
```python
alert = AlertTemplates.high_vix_alert(
    vix_level=32.5,
    max_vix=25.0,
)
```

**Characteristics:**
- Severity: WARNING
- Event type: `high_vix`

### TRADE EVENTS (INFO severity)

#### Trade Entry
```python
alert = AlertTemplates.trade_entry(
    direction="LONG",
    contracts=2,
    entry_price=20000.0,
    stop_loss=19990.0,
    take_profit=20020.0,
    signal_confidence=0.85,
)
```

**Characteristics:**
- Severity: INFO
- Event type: `trade_entry`
- Includes: Risk-reward ratio, signal confidence
- Rate limited: Yes

#### Trade Exit
```python
alert = AlertTemplates.trade_exit(
    direction="LONG",
    contracts=2,
    exit_price=20010.0,
    entry_price=20000.0,
    pnl=20.0,
    exit_reason="Trail stop hit",
)
```

**Characteristics:**
- Severity: INFO
- Event type: `trade_exit`
- Includes: PnL, exit reason
- Rate limited: Yes

#### Partial Exit
```python
alert = AlertTemplates.partial_exit(
    contracts_exited=1,
    remaining_contracts=1,
    exit_price=20015.0,
    pnl=15.0,
)
```

**Characteristics:**
- Severity: INFO
- Event type: `partial_exit`

### DAILY SUMMARY (INFO severity)

#### Daily Summary
```python
alert = AlertTemplates.daily_summary(
    total_trades=10,
    winning_trades=7,
    losing_trades=3,
    daily_pnl=500.0,
    win_rate=70.0,
    profit_factor=1.5,
    largest_win=100.0,
    largest_loss=-50.0,
)
```

**Characteristics:**
- Severity: INFO
- Event type: `daily_summary`
- Sent: Once per day at market close
- Includes: Win rate, profit factor, expectancy

#### Startup/Shutdown
```python
# Startup
alert = AlertTemplates.startup_complete(
    environment="paper",
    broker="tradovate",
)

# Shutdown
alert = AlertTemplates.shutdown_initiated(
    reason="User stopped system",
)
```

---

## Integration Points in Trading Loop

### 1. Risk Engine (risk/engine.py)

When kill switch is triggered:

```python
from monitoring.alert_templates import AlertTemplates

def check_kill_switch(self) -> RiskDecision:
    if self.state.kill_switch_active:
        # Alert already sent when activated, but verify
        alert = AlertTemplates.kill_switch_triggered(
            reason=self.state.kill_switch_reason,
            stats={
                "consecutive_losses": self.state.consecutive_losses,
                "drawdown_pct": self.state.current_drawdown_pct,
                "daily_pnl": self.state.daily_pnl,
            }
        )
        alert_manager = get_alert_manager()
        if alert_manager:
            alert_manager.enqueue(alert)
        return RiskDecision.KILL_SWITCH
```

### 2. Monitoring Engine (monitoring/engine.py)

Update to integrate with AlertManager:

```python
from monitoring.alert_templates import AlertTemplates
from monitoring.alerting import get_alert_manager

class MonitoringEngine:
    def record_trade(self, trade_result: dict) -> None:
        # ... existing code ...
        self._check_alerts(trade_result)

    def _check_alerts(self, trade_result: dict) -> None:
        """Check if any alert conditions are met."""
        alert_mgr = get_alert_manager()
        if not alert_mgr:
            return

        pnl = trade_result.get("pnl", 0.0)

        # Trade exit alert
        if trade_result.get("action") == "exit":
            alert = AlertTemplates.trade_exit(
                direction=trade_result.get("direction", "UNKNOWN"),
                contracts=trade_result.get("contracts", 0),
                exit_price=trade_result.get("exit_price", 0),
                entry_price=trade_result.get("entry_price", 0),
                pnl=pnl,
                exit_reason=trade_result.get("exit_reason", "Manual"),
            )
            alert_mgr.enqueue(alert)

        # Consecutive loss alert
        if pnl < 0:
            self.metrics.losing_trades += 1
            if self.metrics.losing_trades >= 3:
                alert = AlertTemplates.consecutive_loss_streak(
                    consecutive_losses=self.metrics.losing_trades,
                    avg_loss=self.metrics.avg_loser,
                    total_loss=self.metrics.gross_loss,
                )
                alert_mgr.enqueue(alert)
```

### 3. Broker Connection (Broker/tradovate_client.py)

On WebSocket disconnect:

```python
async def _on_websocket_disconnect(self, error=None):
    """Handle WebSocket disconnection."""
    from monitoring.alert_templates import AlertTemplates
    from monitoring.alerting import get_alert_manager

    alert_mgr = get_alert_manager()
    if alert_mgr:
        alert = AlertTemplates.connection_loss(
            component="tradovate_market_data",
            error=str(error) if error else "Connection lost",
        )
        await alert_mgr.send_immediate(alert)  # Use immediate for critical path

    # Then attempt reconnect...
```

### 4. Scale-Out Executor (execution/scale_out_executor.py)

On partial exits:

```python
from monitoring.alert_templates import AlertTemplates
from monitoring.alerting import get_alert_manager

async def execute_scale_out(self, ...):
    # ... exit logic ...

    # Send alert on partial exit
    alert_mgr = get_alert_manager()
    if alert_mgr:
        alert = AlertTemplates.partial_exit(
            contracts_exited=1,
            remaining_contracts=remaining,
            exit_price=exit_price,
            pnl=pnl_on_exit,
        )
        alert_mgr.enqueue(alert)
```

---

## Configuration Reference

### AlertConfig Dataclass

Located in `config/settings.py`:

```python
@dataclass
class AlertConfig:
    enabled_channels: list           # ["console", "discord", "telegram"]
    discord_webhook_url: str         # Discord webhook URL
    telegram_bot_token: str          # Telegram bot token
    telegram_chat_id: str            # Telegram chat ID
    rate_limit_seconds: int          # Rate limit window (default 300)
```

### Environment Variables

```env
# Alerting configuration
ALERT_CHANNELS=console,discord,telegram
DISCORD_WEBHOOK_URL=https://discordapp.com/api/webhooks/...
TELEGRAM_BOT_TOKEN=123:ABC...
TELEGRAM_CHAT_ID=123456789
ALERT_RATE_LIMIT_SECONDS=300
```

---

## Channel Configuration

### Discord

1. **Create Webhook:**
   - Go to Server → Server Settings → Integrations → Webhooks
   - Click "Create Webhook"
   - Name it "NQ Trading Alerts"
   - Select #alerts channel
   - Copy webhook URL

2. **Rich Embeds:**
   - Title, description, color (by severity)
   - Fields for trade details
   - Timestamp
   - Auto-truncates to 4096 chars (Discord limit)

### Telegram

1. **Create Bot:**
   - Message @BotFather on Telegram
   - `/newbot`
   - Follow prompts, get token

2. **Get Chat ID:**
   - Send message to your bot
   - Go to: `https://api.telegram.org/bot{TOKEN}/getUpdates`
   - Find your chat_id in response

3. **Format:**
   - HTML formatting (bold, code)
   - Emoji for severity level
   - Escapes dangerous characters

### Console

- Always available as fallback
- Structured logging with emoji indicators
- Levels: INFO, WARNING, CRITICAL
- Safe for all environments

---

## Rate Limiting Behavior

### Normal Alerts (INFO, WARNING, CRITICAL)

- **Max 1 per event type per 5 minutes** (configurable)
- Different event types have independent limits
- Subsequent alerts within window are silently dropped

```python
# First alert sent
manager.enqueue(alert)  # ✓ Sent

# Same type immediately after - DROPPED (rate limited)
manager.enqueue(alert)  # ✗ Dropped

# Different type - ALLOWED
manager.enqueue(alert_different_type)  # ✓ Sent

# After 5+ minutes - ALLOWED
await asyncio.sleep(301)
manager.enqueue(alert)  # ✓ Sent
```

### Emergency Alerts (EMERGENCY)

- **Bypass rate limiting** - sent immediately
- Use for kill switch only
- Still subject to retry logic (3 attempts with backoff)

```python
# Kill switch alert - ALWAYS sent
emergency_alert = AlertTemplates.kill_switch_triggered(...)
manager.enqueue(emergency_alert)  # ✓ Sent regardless of rate limit
```

---

## Usage Patterns

### Pattern 1: Simple Trade Alert

```python
async def on_trade_entry(self, entry_data):
    """Send trade entry alert."""
    alert = AlertTemplates.trade_entry(
        direction=entry_data["direction"],
        contracts=entry_data["contracts"],
        entry_price=entry_data["entry_price"],
        stop_loss=entry_data["stop_loss"],
        take_profit=entry_data["take_profit"],
        signal_confidence=entry_data["confidence"],
    )
    self.alert_manager.enqueue(alert)  # Non-blocking
```

### Pattern 2: Immediate Critical Alert

```python
async def on_connection_lost(self, error):
    """Send critical alert immediately."""
    alert = AlertTemplates.connection_loss(
        component="broker",
        error=str(error),
    )
    # Use send_immediate for critical paths
    await self.alert_manager.send_immediate(alert)

    # Attempt reconnection...
```

### Pattern 3: Conditional Risk Warning

```python
def check_risk_levels(self, risk_state):
    """Send alerts based on risk thresholds."""
    alert_mgr = get_alert_manager()
    if not alert_mgr:
        return

    # Drawdown warning
    if risk_state["drawdown_pct"] > 2.0:
        alert = AlertTemplates.drawdown_warning(
            current_drawdown_pct=risk_state["drawdown_pct"],
            max_drawdown_pct=risk_state["max_drawdown_pct"],
            daily_pnl=risk_state["daily_pnl"],
        )
        alert_mgr.enqueue(alert)

    # VIX alert
    if risk_state["vix"] > 30:
        alert = AlertTemplates.high_vix_alert(
            vix_level=risk_state["vix"],
            max_vix=25.0,
        )
        alert_mgr.enqueue(alert)
```

### Pattern 4: Custom Alerts

```python
from monitoring.alerting import AlertSeverity
from monitoring.alert_templates import AlertTemplates

# For events not covered by templates
alert = AlertTemplates.custom_alert(
    event_type="position_size_reduced",
    title="Position Size Reduced",
    message="Due to VIX spike: 25.0 → 30.5",
    severity=AlertSeverity.WARNING,
    data={
        "previous_size": 2,
        "new_size": 1,
        "reason": "vix_spike",
        "previous_vix": 25.0,
        "current_vix": 30.5,
    }
)
self.alert_manager.enqueue(alert)
```

---

## Troubleshooting

### Alert Not Received?

1. **Check rate limit:**
   ```python
   # Log last sent time
   print(alert_mgr._last_sent)
   ```

2. **Verify channel configuration:**
   ```python
   health = await alert_mgr.health_check()
   print(health)  # {"console": True, "discord": False, ...}
   ```

3. **Check if manager is running:**
   ```python
   if not alert_mgr._running:
       await alert_mgr.start()
   ```

### Discord Webhook Failing?

- Verify webhook URL is correct
- Check webhook hasn't been deleted
- Verify permissions on Discord channel
- Try sending test message: `curl -X POST {webhook_url} -d '{"content":"Test"}'`

### Telegram Bot Not Working?

- Verify token is correct
- Verify chat_id is correct
- Try test: `https://api.telegram.org/botTOKEN/sendMessage?chat_id=CHATID&text=Test`
- Ensure bot has permission to post in channel

### AsyncIO Errors?

Ensure alert manager is used in async context:

```python
# ✓ Correct
async def main():
    await alert_mgr.start()
    # ...
    await alert_mgr.stop()

asyncio.run(main())

# ✗ Wrong - don't block event loop
await alert_mgr.start()
alert_mgr.enqueue(alert)  # May deadlock
```

---

## Performance Considerations

### Queue Backpressure

If alerts are being dropped from queue:

```python
# Check queue size
print(alert_mgr._queue.qsize())

# Increase queue size if needed (default unlimited)
# In AlertManager.__init__:
self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
```

### Network Timeouts

Exponential backoff with 10-second timeout per channel:

```
Attempt 1: Immediate
Attempt 2: Wait 2s, retry
Attempt 3: Wait 4s, retry
```

### Memory Usage

Alert history not kept by default. To add history:

```python
# In AlertManager
self._history: List[Alert] = []

async def _send_alert(self, alert):
    self._history.append(alert)
    # Keep last 1000
    if len(self._history) > 1000:
        self._history.pop(0)
```

---

## Testing

Run the test suite:

```bash
# Install test dependencies
pip install pytest pytest-asyncio aiohttp

# Run tests
pytest tests/test_alerting.py -v

# Run specific test class
pytest tests/test_alerting.py::TestAlertManagerRateLimiting -v

# Run with coverage
pytest tests/test_alerting.py --cov=monitoring.alerting
```

Key test areas:
- Rate limiting logic
- Channel send/retry
- Queue processing
- Health checks
- Alert templates

---

## Best Practices

1. **Always enqueue, rarely send_immediate:**
   ```python
   # Preferred - never blocks trading loop
   alert_mgr.enqueue(alert)

   # Use sparingly - blocks current coroutine
   await alert_mgr.send_immediate(alert)
   ```

2. **Use templates for standard events:**
   ```python
   # Good - consistent formatting
   alert = AlertTemplates.trade_entry(...)

   # Avoid - inconsistent formatting
   alert = Alert(event_type="custom_entry", ...)
   ```

3. **Include relevant context in data:**
   ```python
   # Good
   alert.data = {
       "current_drawdown": 2.5,
       "max_limit": 3.0,
       "daily_pnl": -1250,
   }

   # Avoid
   alert.data = {"bad": "data"}
   ```

4. **Check if manager exists:**
   ```python
   alert_mgr = get_alert_manager()
   if alert_mgr:
       alert_mgr.enqueue(alert)
   ```

5. **Handle shutdown gracefully:**
   ```python
   finally:
       if self.alert_manager and self.alert_manager._running:
           await self.alert_manager.stop()
   ```

---

## Summary

| Feature | Details |
|---------|---------|
| **Channels** | Console, Discord, Telegram |
| **Severity Levels** | INFO, WARNING, CRITICAL, EMERGENCY |
| **Rate Limiting** | 1 per event type per 5 min (configurable, except EMERGENCY) |
| **Async Queue** | Non-blocking, background worker |
| **Retry Logic** | 3 attempts with exponential backoff |
| **Message Formatting** | Rich Discord embeds, Telegram HTML, console structured logging |
| **Pre-built Templates** | 15+ alert types for all trading events |

Start with console-only alerting, add Discord/Telegram once configured.
