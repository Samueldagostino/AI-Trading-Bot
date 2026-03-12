# Alerting System - Quick Start (5 Minutes)

## Step 1: Configure Environment (1 min)

```bash
# Copy template
cp .env.example .env

# Edit .env and add:
ALERT_CHANNELS=console,discord,telegram
DISCORD_WEBHOOK_URL=https://discordapp.com/api/webhooks/YOUR_ID/YOUR_TOKEN
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
ALERT_RATE_LIMIT_SECONDS=300
```

## Step 2: Initialize in TradingOrchestrator (2 min)

In `main.py`, in `TradingOrchestrator.__init__()`:

```python
from monitoring.alerting import AlertManager, set_alert_manager
from config.settings import CONFIG

class TradingOrchestrator:
    def __init__(self, config: BotConfig = CONFIG):
        # ... existing code ...
        
        # Initialize alerting
        self.alert_manager = AlertManager(config.alerting)
        set_alert_manager(self.alert_manager)
```

## Step 3: Start/Stop Manager (1 min)

In your async `run()` method:

```python
async def run(self):
    await self.alert_manager.start()  # Start at beginning
    
    try:
        while self.running:
            # Trading loop...
            pass
    finally:
        await self.alert_manager.stop()  # Stop at end
```

## Step 4: Send Alerts from Code (1 min)

Anywhere in trading loop:

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
    reason="Max consecutive losses",
    stats={"consecutive_losses": 5},
)
self.alert_manager.enqueue(alert)
```

## Available Alert Templates

```python
AlertTemplates.kill_switch_triggered(reason, stats)
AlertTemplates.connection_loss(component, error)
AlertTemplates.system_error(component, error)
AlertTemplates.drawdown_warning(current_pct, max_pct, daily_pnl)
AlertTemplates.consecutive_loss_streak(count, avg_loss, total_loss)
AlertTemplates.high_vix_alert(vix_level, max_vix)
AlertTemplates.trade_entry(direction, contracts, entry, stop, target, confidence)
AlertTemplates.trade_exit(direction, contracts, exit_price, entry_price, pnl, reason)
AlertTemplates.partial_exit(exited, remaining, exit_price, pnl)
AlertTemplates.daily_summary(total, wins, losses, pnl, win_rate, pf, best, worst)
AlertTemplates.startup_complete(environment, broker)
AlertTemplates.shutdown_initiated(reason)
AlertTemplates.custom_alert(event_type, title, message, severity, data)
```

## Key Points

1. **Non-blocking:** `enqueue()` returns immediately, never blocks trading
2. **Rate limiting:** Max 1 per event type per 5 minutes (EMERGENCY bypasses)
3. **Channels:** Console always on, Discord/Telegram optional
4. **Retry:** Automatic retry with exponential backoff
5. **Templates:** Use pre-built templates for consistency

## Testing

```bash
# Check syntax
python -m py_compile monitoring/alerting.py

# Run tests (after installing pytest + aiohttp)
pytest tests/test_alerting.py -v
```

## Integration Points

| Module | Event | Alert |
|--------|-------|-------|
| Risk Engine | Kill switch | `kill_switch_triggered` |
| Monitoring | Trade exit | `trade_exit` |
| Monitoring | Consecutive losses | `consecutive_loss_streak` |
| Broker | Disconnect | `connection_loss` |
| ScaleOut | Partial exit | `partial_exit` |
| Main | Startup | `startup_complete` |
| Main | Shutdown | `shutdown_initiated` |

## Troubleshooting

**Alert not received?**
1. Check rate limit: `print(alert_mgr._last_sent)`
2. Verify health: `health = await alert_mgr.health_check(); print(health)`
3. Check if running: `if not alert_mgr._running: await alert_mgr.start()`

**Discord webhook failing?**
1. Verify URL is correct: `curl -X POST {url} -d '{"content":"Test"}'`
2. Check webhook hasn't been deleted
3. Check channel permissions

**Telegram not working?**
1. Verify bot token: `https://api.telegram.org/botTOKEN/getMe`
2. Verify chat_id: `https://api.telegram.org/botTOKEN/sendMessage?chat_id=CHATID&text=Test`

## Full Documentation

See `ALERTING_GUIDE.md` for complete reference.

## Summary

- 4 files created: alerting.py, alert_templates.py, test_alerting.py, ALERTING_GUIDE.md
- 2 files modified: config/settings.py, .env.example
- 1,560+ lines of code
- 40+ unit tests
- 0 new dependencies (aiohttp already used)

**Status: Ready for integration ✓**
