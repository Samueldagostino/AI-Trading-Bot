"""
Dashboard Web Server
=====================
FastAPI server providing:
- REST API for system status, trades, signals
- WebSocket for real-time updates
- Static file serving for the HTML dashboard

Run with:
  uvicorn dashboard.server:app --reload --port 8080
  Then open http://localhost:8080
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NQ Trading Bot — Dashboard",
    version="1.0.0",
    description="Real-time monitoring dashboard for the NQ Futures trading system",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# Simulated state (replace with real orchestrator in production)
# ================================================================
# This demo state lets you see the dashboard immediately.
# In production, this connects to the live TradingOrchestrator.

DEMO_STATE = {
    "running": True,
    "environment": "paper",
    "bars_processed": 0,
    "current_regime": "unknown",
    "uptime_seconds": 0,
    "risk_state": {
        "equity": 50000.00,
        "starting_equity": 50000.00,
        "peak_equity": 50000.00,
        "daily_pnl": 0.00,
        "drawdown_pct": 0.00,
        "max_drawdown_pct": 0.00,
        "consecutive_losses": 0,
        "consecutive_wins": 0,
        "kill_switch_active": False,
        "daily_limit_hit": False,
        "is_overnight": False,
        "vix": 18.5,
        "daily_trades": 0,
        "daily_win_rate": 0.0,
    },
    "signal_stats": {
        "total_signals_evaluated": 0,
        "trade_signals_generated": 0,
        "signal_rate": 0.0,
        "avg_confluence_score": 0.0,
    },
    "execution_stats": {
        "total_orders": 0,
        "filled_orders": 0,
        "fill_rate": 0.0,
        "avg_slippage_points": 0.0,
        "total_commission": 0.0,
    },
    "performance": {
        "total_trades": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "profit_factor": 0.0,
        "avg_winner": 0.0,
        "avg_loser": 0.0,
        "expectancy": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
    },
    "has_open_position": False,
    "open_position": None,
    "discord_connected": False,
    "recent_trades": [],
    "recent_signals": [],
    "equity_curve": [50000.00],
    "health": {
        "data": "healthy",
        "features": "healthy",
        "signals": "healthy",
        "risk": "healthy",
        "execution": "healthy",
        "discord": "offline",
    },
    "alerts": [],
}

# Track connected WebSocket clients
connected_clients: list = []
_start_time = datetime.now(timezone.utc)


# ================================================================
# REST API Endpoints
# ================================================================

@app.get("/")
async def serve_dashboard():
    """Serve the main dashboard HTML."""
    html_path = Path(__file__).parent / "static" / "index.html"
    return FileResponse(html_path)


@app.get("/api/status")
async def get_status():
    """Full system status snapshot."""
    DEMO_STATE["uptime_seconds"] = int(
        (datetime.now(timezone.utc) - _start_time).total_seconds()
    )
    return JSONResponse(DEMO_STATE)


@app.get("/api/risk")
async def get_risk():
    """Current risk state."""
    return JSONResponse(DEMO_STATE["risk_state"])


@app.get("/api/performance")
async def get_performance():
    """Performance metrics."""
    return JSONResponse(DEMO_STATE["performance"])


@app.get("/api/trades")
async def get_trades():
    """Recent trade history."""
    return JSONResponse(DEMO_STATE["recent_trades"])


@app.get("/api/signals")
async def get_signals():
    """Recent signals."""
    return JSONResponse(DEMO_STATE["recent_signals"])


@app.get("/api/health")
async def get_health():
    """Component health status."""
    return JSONResponse(DEMO_STATE["health"])


@app.post("/api/kill-switch")
async def toggle_kill_switch():
    """Manually activate kill switch."""
    DEMO_STATE["risk_state"]["kill_switch_active"] = True
    await broadcast({"type": "kill_switch", "active": True})
    return JSONResponse({"status": "kill_switch_activated"})


@app.post("/api/kill-switch/reset")
async def reset_kill_switch():
    """Reset kill switch."""
    DEMO_STATE["risk_state"]["kill_switch_active"] = False
    await broadcast({"type": "kill_switch", "active": False})
    return JSONResponse({"status": "kill_switch_reset"})


# ================================================================
# WebSocket for real-time updates
# ================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    logger.info(f"WebSocket client connected. Total: {len(connected_clients)}")

    try:
        # Send initial state
        await websocket.send_json({"type": "full_state", "data": DEMO_STATE})
        
        # Keep connection alive, send heartbeat
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
                # Handle incoming commands from dashboard
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Send heartbeat
                DEMO_STATE["uptime_seconds"] = int(
                    (datetime.now(timezone.utc) - _start_time).total_seconds()
                )
                await websocket.send_json({
                    "type": "heartbeat",
                    "data": {
                        "uptime": DEMO_STATE["uptime_seconds"],
                        "bars_processed": DEMO_STATE["bars_processed"],
                        "regime": DEMO_STATE["current_regime"],
                    }
                })
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total: {len(connected_clients)}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in connected_clients:
            connected_clients.remove(websocket)


async def broadcast(message: dict):
    """Broadcast a message to all connected WebSocket clients."""
    for client in connected_clients[:]:
        try:
            await client.send_json(message)
        except Exception:
            connected_clients.remove(client)


# ================================================================
# Static files
# ================================================================
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
