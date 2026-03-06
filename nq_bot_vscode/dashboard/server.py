"""
Dashboard Web Server — Real-Time Monitoring Hub
=================================================
FastAPI server providing:
- REST API for equity, regime, signals, execution quality, alerts, HTF, WF, contract
- WebSocket for real-time push updates (bar, signal, trade, regime, alert, pnl)
- Static file serving + Jinja2 templates
- Token-based authentication (Bearer token for API, login page for browser)

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
from typing import Optional, List
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.templating import Jinja2Templates

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NQ Trading Bot — Dashboard",
    version="2.0.0",
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
# Templates
# ================================================================
_base_dir = Path(__file__).parent
_templates_dir = _base_dir / "templates"
_templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(_templates_dir))

# ================================================================
# Auth — requires DASHBOARD_API_TOKEN env var
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


async def _optional_token(authorization: str = Header(default="")) -> bool:
    """Check token without raising — returns True if valid."""
    if not authorization.startswith("Bearer "):
        return False
    return secrets.compare_digest(authorization[7:], _DASHBOARD_TOKEN)


# ================================================================
# Simulated state (replace with real orchestrator in production)
# ================================================================
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
    "regime_history": [],
    "signal_heatmap": {
        "technical": [0] * 20,
        "discord": [0] * 20,
        "ml": [0] * 20,
        "sweep": [0] * 20,
        "htf": [0] * 20,
    },
    "htf_bias": {
        "1D": {"direction": "neutral", "strength": 0.0},
        "4H": {"direction": "neutral", "strength": 0.0},
        "1H": {"direction": "neutral", "strength": 0.0},
        "30m": {"direction": "neutral", "strength": 0.0},
        "15m": {"direction": "neutral", "strength": 0.0},
        "5m": {"direction": "neutral", "strength": 0.0},
    },
    "contract": {
        "symbol": "MNQH6",
        "expiry": "2026-03-20",
        "days_until_expiry": 14,
        "roll_date": "2026-03-13",
    },
    "walk_forward": {
        "last_run": None,
        "status": "not_run",
        "pf": 0.0,
        "win_rate": 0.0,
    },
    "execution_history": [],
}

# Track connected WebSocket clients
connected_clients: list = []
_start_time = datetime.now(timezone.utc)


# ================================================================
# Login / Auth endpoints
# ================================================================

@app.get("/login")
async def login_page():
    """Serve the login page."""
    html_path = _templates_dir / "login.html"
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>Login</h1><p>login.html template not found</p>", status_code=500)


@app.post("/api/login")
async def api_login(request: Request):
    """Validate token and return it for localStorage storage."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    token = body.get("token", "")
    if not token or not secrets.compare_digest(token, _DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")
    return JSONResponse({"token": token, "status": "ok"})


# ================================================================
# Dashboard HTML
# ================================================================

@app.get("/")
async def serve_dashboard(request: Request):
    """Serve the main dashboard HTML."""
    # Try templates/index.html first, then static/index.html
    template_path = _templates_dir / "index.html"
    if template_path.exists():
        return FileResponse(template_path)
    static_path = _base_dir / "static" / "index.html"
    if static_path.exists():
        return FileResponse(static_path)
    return HTMLResponse("<h1>Dashboard</h1><p>index.html not found</p>", status_code=500)


# ================================================================
# REST API Endpoints — Original
# ================================================================

@app.get("/api/status", dependencies=[Depends(_require_token)])
async def get_status():
    """Full system status snapshot."""
    DEMO_STATE["uptime_seconds"] = int(
        (datetime.now(timezone.utc) - _start_time).total_seconds()
    )
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


@app.get("/api/risk", dependencies=[Depends(_require_token)])
async def get_risk():
    """Current risk state."""
    return JSONResponse(DEMO_STATE["risk_state"])


@app.get("/api/performance", dependencies=[Depends(_require_token)])
async def get_performance():
    """Performance metrics."""
    return JSONResponse(DEMO_STATE["performance"])


@app.get("/api/trades", dependencies=[Depends(_require_token)])
async def get_trades():
    """Recent trade history."""
    order_mgr = app.state.__dict__.get("order_manager")
    if order_mgr is not None:
        try:
            active = order_mgr.get_active_positions()
            history = order_mgr.get_trade_history()[-20:]
            return JSONResponse({
                "active_positions": active,
                "recent_trades": history,
            })
        except Exception:
            pass
    return JSONResponse({"recent_trades": DEMO_STATE["recent_trades"]})


@app.get("/api/signals", dependencies=[Depends(_require_token)])
async def get_signals():
    """Recent signals."""
    return JSONResponse(DEMO_STATE["recent_signals"])


@app.get("/api/health")
async def get_health():
    """Component health status (no auth required for monitoring probes)."""
    return JSONResponse(DEMO_STATE["health"])


@app.get("/api/gamma", dependencies=[Depends(_require_token)])
async def get_gamma():
    """Gamma exposure (GEX) data."""
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
    """Manually activate kill switch."""
    DEMO_STATE["risk_state"]["kill_switch_active"] = True
    await broadcast({"type": "kill_switch", "active": True})
    return JSONResponse({"status": "kill_switch_activated"})


@app.post("/api/kill-switch/reset", dependencies=[Depends(_require_token)])
async def reset_kill_switch():
    """Reset kill switch."""
    DEMO_STATE["risk_state"]["kill_switch_active"] = False
    await broadcast({"type": "kill_switch", "active": False})
    return JSONResponse({"status": "kill_switch_reset"})


# ================================================================
# NEW API Endpoints — Dashboard Overhaul
# ================================================================

@app.get("/api/equity-curve", dependencies=[Depends(_require_token)])
async def get_equity_curve():
    """Historical equity data points."""
    curve = DEMO_STATE.get("equity_curve", [50000.00])
    now = datetime.now(timezone.utc)
    points = []
    for i, val in enumerate(curve):
        ts = now - timedelta(days=len(curve) - 1 - i)
        points.append({
            "timestamp": ts.isoformat(),
            "date": ts.strftime("%Y-%m-%d"),
            "equity": val,
        })

    # Try monitoring engine
    mon = app.state.__dict__.get("monitoring_engine")
    if mon is not None:
        try:
            dash_data = mon.get_dashboard_data()
            if "equity_curve" in dash_data:
                points = dash_data["equity_curve"]
        except Exception:
            pass

    return JSONResponse({"points": points, "count": len(points)})


@app.get("/api/regime", dependencies=[Depends(_require_token)])
async def get_regime():
    """Current regime state + history."""
    return JSONResponse({
        "current_state": DEMO_STATE.get("current_regime", "unknown"),
        "vix": DEMO_STATE["risk_state"].get("vix"),
        "history": DEMO_STATE.get("regime_history", []),
    })


@app.get("/api/signals/heatmap", dependencies=[Depends(_require_token)])
async def get_signals_heatmap():
    """Signal component scores over time (last 20 bars)."""
    return JSONResponse(DEMO_STATE.get("signal_heatmap", {
        "technical": [0] * 20,
        "discord": [0] * 20,
        "ml": [0] * 20,
        "sweep": [0] * 20,
        "htf": [0] * 20,
    }))


@app.get("/api/execution/quality", dependencies=[Depends(_require_token)])
async def get_execution_quality():
    """Slippage, latency, fill rates."""
    # Try execution analytics engine
    analytics = app.state.__dict__.get("execution_analytics")
    if analytics is not None:
        try:
            rolling = analytics.get_rolling_metrics()
            history = []
            for event in list(analytics._rolling):
                if event.fill_at and event.slippage_ticks is not None:
                    history.append({
                        "timestamp": event.fill_at.isoformat(),
                        "slippage_ticks": event.slippage_ticks,
                        "latency_ms": event.latency_ms,
                    })
            return JSONResponse({
                "rolling": rolling,
                "fill_rate": analytics.get_fill_rate(),
                "history": history,
            })
        except Exception:
            pass

    return JSONResponse({
        "rolling": {
            "avg_slippage_ticks": DEMO_STATE["execution_stats"].get("avg_slippage_points", 0),
            "avg_latency_ms": 0,
            "fill_rate": DEMO_STATE["execution_stats"].get("fill_rate", 0),
            "count": 0,
        },
        "fill_rate": DEMO_STATE["execution_stats"].get("fill_rate", 0),
        "history": DEMO_STATE.get("execution_history", []),
    })


@app.get("/api/alerts/history", dependencies=[Depends(_require_token)])
async def get_alerts_history():
    """Recent alerts with severity."""
    alerts = DEMO_STATE.get("alerts", [])

    # Try alert manager
    mon = app.state.__dict__.get("monitoring_engine")
    if mon is not None:
        try:
            dash_data = mon.get_dashboard_data()
            alerts = dash_data.get("recent_alerts", alerts)
        except Exception:
            pass

    return JSONResponse({"alerts": alerts[-20:], "count": len(alerts)})


@app.get("/api/htf-bias", dependencies=[Depends(_require_token)])
async def get_htf_bias():
    """Current HTF consensus across timeframes."""
    htf = DEMO_STATE.get("htf_bias", {})

    # Try HTF engine from app state
    htf_engine = app.state.__dict__.get("htf_engine")
    if htf_engine is not None:
        try:
            for tf in ["1D", "4H", "1H", "30m", "15m", "5m"]:
                bias = htf_engine.get_bias(tf)
                if bias:
                    htf[tf] = {"direction": bias.get("direction", "neutral"),
                               "strength": bias.get("strength", 0.0)}
        except Exception:
            pass

    return JSONResponse({"timeframes": htf})


@app.get("/api/walk-forward/latest", dependencies=[Depends(_require_token)])
async def get_walk_forward():
    """Latest walk-forward validation results."""
    return JSONResponse(DEMO_STATE.get("walk_forward", {
        "last_run": None,
        "status": "not_run",
        "pf": 0.0,
        "win_rate": 0.0,
    }))


@app.get("/api/contract/status", dependencies=[Depends(_require_token)])
async def get_contract_status():
    """Current contract, roll date, days until expiry."""
    contract = DEMO_STATE.get("contract", {})

    # Try contract roller from app state
    roller = app.state.__dict__.get("contract_roller")
    if roller is not None:
        try:
            info = roller.get_status()
            contract.update(info)
        except Exception:
            pass

    # Compute days until expiry from expiry date
    if contract.get("expiry"):
        try:
            expiry_date = datetime.strptime(contract["expiry"], "%Y-%m-%d").date()
            contract["days_until_expiry"] = (expiry_date - datetime.now(timezone.utc).date()).days
        except Exception:
            pass

    return JSONResponse(contract)


# ================================================================
# WebSocket for real-time updates
# ================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Extract token from query params
    query = websocket.scope.get("query_string", b"").decode()
    params = parse_qs(query)
    token_list = params.get("token", [])
    ws_token = token_list[0] if token_list else ""

    if not ws_token or not secrets.compare_digest(ws_token, _DASHBOARD_TOKEN):
        # Accept then close with auth error for cleaner client handling
        await websocket.accept()
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    connected_clients.append(websocket)
    logger.info("WebSocket client connected. Total: %d", len(connected_clients))

    try:
        # Send initial state
        await websocket.send_json({"type": "full_state", "data": DEMO_STATE})

        # Keep connection alive
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
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
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        logger.info("WebSocket client disconnected. Total: %d", len(connected_clients))
    except Exception as e:
        logger.error("WebSocket error: %s", e)
        if websocket in connected_clients:
            connected_clients.remove(websocket)


async def broadcast(message: dict):
    """Broadcast a message to all connected WebSocket clients."""
    for client in connected_clients[:]:
        try:
            await client.send_json(message)
        except Exception:
            if client in connected_clients:
                connected_clients.remove(client)


# ================================================================
# Push helpers — called by trading pipeline
# ================================================================

async def push_bar(price: float, volume: int = 0, vwap: float = 0.0, timestamp: str = ""):
    """Push a bar update to all clients."""
    await broadcast({
        "type": "bar",
        "data": {
            "price": price,
            "volume": volume,
            "vwap": vwap,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        }
    })


async def push_signal(direction: str, score: float, source: str = "confluence"):
    """Push a signal event."""
    await broadcast({
        "type": "signal",
        "data": {"direction": direction, "score": score, "source": source}
    })


async def push_trade(action: str, direction: str, price: float):
    """Push a trade event (entry/exit)."""
    await broadcast({
        "type": "trade",
        "data": {"action": action, "direction": direction, "price": price}
    })


async def push_regime(state: str, vix: float = 0.0):
    """Push a regime change."""
    DEMO_STATE["current_regime"] = state
    await broadcast({
        "type": "regime",
        "data": {"state": state, "vix": vix}
    })


async def push_alert(severity: str, message: str):
    """Push an alert."""
    alert = {
        "severity": severity,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    DEMO_STATE["alerts"].append(alert)
    if len(DEMO_STATE["alerts"]) > 100:
        DEMO_STATE["alerts"] = DEMO_STATE["alerts"][-100:]
    await broadcast({"type": "alert", "data": alert})


async def push_pnl(daily: float, unrealized: float = 0.0):
    """Push PnL update."""
    await broadcast({
        "type": "pnl",
        "data": {"daily": daily, "unrealized": unrealized}
    })


# ================================================================
# Static files
# ================================================================
_static_path = _base_dir / "static"
if _static_path.exists():
    app.mount("/static", StaticFiles(directory=str(_static_path)), name="static")
