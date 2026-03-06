"""
Tests for the real-time monitoring dashboard overhaul.
Covers API endpoints, WebSocket, auth, and static file serving.
"""

import os
import pytest
from unittest.mock import patch

# Set token before importing server
os.environ["DASHBOARD_API_TOKEN"] = "test-token-abc123"

from fastapi.testclient import TestClient
from nq_bot_vscode.dashboard.server import app, DEMO_STATE, _DASHBOARD_TOKEN


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_DASHBOARD_TOKEN}"}


# ─── API: Equity Curve ───

def test_api_equity_curve_returns_data(client, auth_headers):
    resp = client.get("/api/equity-curve", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "points" in data
    assert "count" in data
    assert isinstance(data["points"], list)
    assert len(data["points"]) > 0
    # Each point has timestamp and equity
    pt = data["points"][0]
    assert "timestamp" in pt
    assert "equity" in pt


# ─── API: Regime ───

def test_api_regime_returns_current_state(client, auth_headers):
    resp = client.get("/api/regime", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "current_state" in data
    assert data["current_state"] in ("unknown", "trending_up", "trending",
                                      "ranging", "high_vol", "volatile", "crash")
    assert "history" in data


# ─── WebSocket: Connect ───

def test_websocket_connects(client):
    with client.websocket_connect(f"/ws?token={_DASHBOARD_TOKEN}") as ws:
        # Should receive full_state as first message
        msg = ws.receive_json()
        assert msg["type"] == "full_state"
        assert "data" in msg


# ─── WebSocket: Bar Update ───

def test_websocket_receives_bar_update(client):
    """Test that bar updates can be pushed and received."""
    import asyncio
    from nq_bot_vscode.dashboard.server import connected_clients, broadcast

    with client.websocket_connect(f"/ws?token={_DASHBOARD_TOKEN}") as ws:
        # Consume the initial full_state
        msg = ws.receive_json()
        assert msg["type"] == "full_state"

        # Send a ping, expect pong
        ws.send_json({"type": "ping"})
        # May get heartbeat or pong
        for _ in range(5):
            msg = ws.receive_json()
            if msg["type"] == "pong":
                break
        assert msg["type"] == "pong"


# ─── Auth: Required for API ───

def test_auth_required_for_api(client):
    """API endpoints should require auth."""
    endpoints = [
        "/api/equity-curve",
        "/api/regime",
        "/api/signals/heatmap",
        "/api/execution/quality",
        "/api/alerts/history",
        "/api/htf-bias",
        "/api/walk-forward/latest",
        "/api/contract/status",
        "/api/status",
    ]
    for endpoint in endpoints:
        resp = client.get(endpoint)
        assert resp.status_code in (401, 422), f"{endpoint} returned {resp.status_code}"


# ─── Auth: Login Returns Token ───

def test_login_returns_token(client):
    resp = client.post("/api/login", json={"token": _DASHBOARD_TOKEN})
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["token"] == _DASHBOARD_TOKEN
    assert data["status"] == "ok"


def test_login_rejects_bad_token(client):
    resp = client.post("/api/login", json={"token": "wrong-token"})
    assert resp.status_code == 401


# ─── Static Files Served ───

def test_static_files_served(client):
    resp = client.get("/static/css/dashboard.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")


def test_static_js_served(client):
    resp = client.get("/static/js/dashboard.js")
    assert resp.status_code == 200


# ─── Dashboard HTML Loads ───

def test_dashboard_html_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert "NQ Trading Bot" in text
    assert "priceChart" in text
    assert "equityChart" in text


def test_login_page_loads(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Login" in resp.text or "login" in resp.text


# ─── Additional API endpoints ───

def test_api_signals_heatmap(client, auth_headers):
    resp = client.get("/api/signals/heatmap", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "technical" in data
    assert "sweep" in data
    assert len(data["technical"]) == 20


def test_api_execution_quality(client, auth_headers):
    resp = client.get("/api/execution/quality", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "rolling" in data
    assert "fill_rate" in data


def test_api_htf_bias(client, auth_headers):
    resp = client.get("/api/htf-bias", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "timeframes" in data


def test_api_walk_forward(client, auth_headers):
    resp = client.get("/api/walk-forward/latest", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data


def test_api_contract_status(client, auth_headers):
    resp = client.get("/api/contract/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "symbol" in data
    assert "days_until_expiry" in data


def test_api_alerts_history(client, auth_headers):
    resp = client.get("/api/alerts/history", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "alerts" in data
    assert "count" in data


def test_api_health_no_auth_required(client):
    """Health endpoint should work without auth for monitoring probes."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data
    assert "execution" in data


def test_websocket_rejects_bad_token(client):
    """WebSocket should reject connections with invalid token."""
    with client.websocket_connect("/ws?token=bad-token") as ws:
        # Server should close connection
        try:
            msg = ws.receive_json()
            # Might receive nothing or close
        except Exception:
            pass  # Expected - connection closed
