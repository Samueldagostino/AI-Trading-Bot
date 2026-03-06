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
import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
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
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# ================================================================
# Kill-switch auth — requires DASHBOARD_API_TOKEN env var
# ================================================================
_DASHBOARD_TOKEN = os.environ.get("DASHBOARD_API_TOKEN", "")
if not _DASHBOARD_TOKEN:
    _DASHBOARD_TOKEN = secrets.token_hex(32)
    logger.warning(
        "DASHBOARD_API_TOKEN not set — generated ephemeral token: %s",
        _DASHBOARD_TOKEN,
    )


async def _require_token(authorization: str = Header(...)) -> None:
    """Validate Bearer token for privileged endpoints."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if not secrets.compare_digest(authorization[7:], _DASHBOARD_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")

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
    """Full system status snapshot — includes trade metrics from OrderManager."""
    DEMO_STATE["uptime_seconds"] = int(
        (datetime.now(timezone.utc) - _start_time).total_seconds()
    )
    # Overlay live trade metrics from OrderManager if available
    order_mgr = app.state.__dict__.get("order_manager")
    if order_mgr is not None:
        try:
            metrics = order_mgr.get_trade_metrics()
            DEMO_STATE["performance"]["total_trades"] = metrics["total_trades"]
            DEMO_STATE["performance"]["win_rate"] = metrics["win_rate"]
            DEMO_STATE["performance"]["total_pnl"] = metrics["total_pnl"]
            DEMO_STATE["risk_state"]["daily_pnl"] = metrics["daily_pnl"]
            DEMO_STATE["risk_state"]["equity"] = metrics["current_equity"]
            DEMO_STATE["risk_state"]["peak_equity"] = metrics["peak_equity"]
            DEMO_STATE["risk_state"]["consecutive_losses"] = metrics["consecutive_losses"]
            DEMO_STATE["execution_stats"]["avg_slippage_points"] = metrics["avg_slippage"]
            DEMO_STATE["has_open_position"] = metrics["active_positions"] > 0
            DEMO_STATE["open_position"] = (
                order_mgr.get_active_positions()[0] if metrics["active_positions"] > 0 else None
            )
        except Exception:
            pass
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
    """Recent trade history — returns live position data from OrderManager if available."""
    # Try to get live data from OrderManager
    order_mgr = app.state.__dict__.get("order_manager")
    if order_mgr is not None:
        try:
            active = order_mgr.get_active_positions()
            history = order_mgr.get_trade_history()[-20:]  # Last 20 trades
            return JSONResponse({
                "active_positions": active,
                "recent_trades": history,
            })
        except Exception:
            pass
    return JSONResponse(DEMO_STATE["recent_trades"])


@app.get("/api/signals")
async def get_signals():
    """Recent signals."""
    return JSONResponse(DEMO_STATE["recent_signals"])


@app.get("/api/health")
async def get_health():
    """Component health status."""
    return JSONResponse(DEMO_STATE["health"])


@app.get("/api/gamma")
async def get_gamma():
    """Gamma exposure (GEX) data from Quant Data API."""
    gamma_state = DEMO_STATE.get("gamma", {
        "enabled": False,
        "regime": "UNKNOWN",
        "net_gex": 0,
        "net_gex_display": "N/A",
        "gamma_flip": None,
        "call_wall": None,
        "put_wall": None,
        "modifier_value": 1.0,
        "last_update": None,
    })
    return JSONResponse(gamma_state)


@app.post("/api/kill-switch", dependencies=[Depends(_require_token)])
async def toggle_kill_switch():
    """Manually activate kill switch (requires Bearer token)."""
    DEMO_STATE["risk_state"]["kill_switch_active"] = True
    await broadcast({"type": "kill_switch", "active": True})
    return JSONResponse({"status": "kill_switch_activated"})


@app.post("/api/kill-switch/reset", dependencies=[Depends(_require_token)])
async def reset_kill_switch():
    """Reset kill switch (requires Bearer token)."""
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
