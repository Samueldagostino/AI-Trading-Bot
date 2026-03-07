"""
Tests for Live Dashboard — HTTP Server + JSON File Reading
============================================================
Covers:
  - API endpoints return valid JSON
  - Empty/missing log file handling
  - Atomic write pattern
  - Dashboard HTML serving
"""

import json
import os
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

# Patch LOGS_DIR before importing the module
_test_log_dir = None


@pytest.fixture(autouse=True)
def patch_logs_dir(tmp_path):
    """Redirect all log reads to a temp directory."""
    global _test_log_dir
    _test_log_dir = tmp_path
    with patch("scripts.live_dashboard.LOGS_DIR", tmp_path):
        yield tmp_path


# ── Import after setup ──
from scripts.live_dashboard import (
    DashboardServer,
    DashboardHandler,
    _read_json_file,
    atomic_write_json,
    DEFAULTS,
    FILE_MAP,
)


# =====================================================================
#  _read_json_file
# =====================================================================
class TestReadJsonFile:

    def test_missing_file_returns_default(self, tmp_path):
        result = _read_json_file(tmp_path / "nonexistent.json", "status")
        assert result == DEFAULTS["status"]

    def test_empty_file_returns_default(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text("")
        result = _read_json_file(f, "status")
        assert result == DEFAULTS["status"]

    def test_valid_json_file(self, tmp_path):
        f = tmp_path / "data.json"
        data = {"trade_count": 5, "wins": 3, "losses": 2}
        f.write_text(json.dumps(data))
        result = _read_json_file(f, "status")
        assert result["trade_count"] == 5

    def test_jsonl_file(self, tmp_path):
        f = tmp_path / "decisions.json"
        lines = [
            json.dumps({"decision": "APPROVED", "id": str(i)})
            for i in range(60)
        ]
        f.write_text("\n".join(lines))
        result = _read_json_file(f, "decisions", is_jsonl=True, limit=50)
        assert len(result) == 50
        # Should return last 50 (ids 10-59)
        assert result[0]["id"] == "10"
        assert result[-1]["id"] == "59"

    def test_jsonl_with_bad_lines(self, tmp_path):
        f = tmp_path / "decisions.json"
        f.write_text('{"a":1}\nBAD_LINE\n{"b":2}\n')
        result = _read_json_file(f, "decisions", is_jsonl=True)
        assert len(result) == 2
        assert result[0] == {"a": 1}
        assert result[1] == {"b": 2}

    def test_invalid_json_returns_default(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        result = _read_json_file(f, "candles")
        assert result == DEFAULTS["candles"]


# =====================================================================
#  atomic_write_json
# =====================================================================
class TestAtomicWrite:

    def test_atomic_write_creates_file(self, tmp_path):
        f = tmp_path / "output.json"
        data = {"key": "value", "num": 42}
        atomic_write_json(f, data)
        assert f.exists()
        loaded = json.loads(f.read_text())
        assert loaded["key"] == "value"
        assert loaded["num"] == 42

    def test_atomic_write_no_tmp_leftover(self, tmp_path):
        f = tmp_path / "output.json"
        atomic_write_json(f, {"a": 1})
        tmp_file = f.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_atomic_write_overwrites(self, tmp_path):
        f = tmp_path / "output.json"
        atomic_write_json(f, {"v": 1})
        atomic_write_json(f, {"v": 2})
        loaded = json.loads(f.read_text())
        assert loaded["v"] == 2

    def test_atomic_write_handles_list(self, tmp_path):
        f = tmp_path / "list.json"
        data = [{"time": "2026-01-01", "o": 100, "h": 105, "l": 99, "c": 103}]
        atomic_write_json(f, data)
        loaded = json.loads(f.read_text())
        assert isinstance(loaded, list)
        assert loaded[0]["o"] == 100


# =====================================================================
#  DashboardServer — HTTP endpoints
# =====================================================================
class TestDashboardServer:

    @pytest.fixture(scope="class")
    def server(self):
        """Start a test server on a random-ish port."""
        port = 18080
        srv = DashboardServer(port=port)
        # Patch LOGS_DIR for the handler
        with patch("scripts.live_dashboard.LOGS_DIR", Path(tempfile.mkdtemp())):
            srv.start(blocking=False)
            time.sleep(0.3)
            yield srv, port
            srv.stop()

    def _get(self, port, path):
        url = f"http://localhost:{port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_root_serves_html(self, server):
        srv, port = server
        status, body = self._get(port, "/")
        assert status == 200
        assert "NQ.BOT" in body
        assert "<!DOCTYPE html>" in body

    def test_api_status_returns_json(self, server):
        srv, port = server
        status, body = self._get(port, "/api/status")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_api_decisions_returns_list(self, server):
        srv, port = server
        status, body = self._get(port, "/api/decisions")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)

    def test_api_candles_returns_json(self, server):
        srv, port = server
        status, body = self._get(port, "/api/candles")
        assert status == 200
        data = json.loads(body)
        # Should be list (default empty) or list of candles
        assert isinstance(data, list)

    def test_api_trades_returns_json(self, server):
        srv, port = server
        status, body = self._get(port, "/api/trades")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)

    def test_api_modifiers_returns_json(self, server):
        srv, port = server
        status, body = self._get(port, "/api/modifiers")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_api_safety_returns_json(self, server):
        srv, port = server
        status, body = self._get(port, "/api/safety")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_404_on_unknown_path(self, server):
        srv, port = server
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._get(port, "/unknown")
        assert exc_info.value.code == 404


# =====================================================================
#  API with populated data
# =====================================================================
class TestDashboardWithData:

    @pytest.fixture
    def server_with_data(self, tmp_path):
        """Start server with pre-populated log files."""
        # Write test data
        state = {"trade_count": 10, "wins": 7, "losses": 3, "total_pnl": 150.0}
        (tmp_path / "paper_trading_state.json").write_text(json.dumps(state))

        candles = [
            {"time": "2026-03-04T10:00:00", "o": 24500, "h": 24510, "l": 24490, "c": 24505, "vol": 1200},
            {"time": "2026-03-04T10:02:00", "o": 24505, "h": 24515, "l": 24500, "c": 24510, "vol": 1100},
        ]
        (tmp_path / "candle_buffer.json").write_text(json.dumps(candles))

        decisions = [
            json.dumps({"decision": "APPROVED", "signal_direction": "LONG", "price_at_signal": 24500}),
            json.dumps({"decision": "REJECTED", "signal_direction": "SHORT", "price_at_signal": 24510}),
        ]
        (tmp_path / "trade_decisions.json").write_text("\n".join(decisions))

        safety = {"daily_pnl": -100.0, "daily_limit": 500.0, "consec_losses": 1, "all_ok": True}
        (tmp_path / "safety_state.json").write_text(json.dumps(safety))

        port = 18081
        with patch("scripts.live_dashboard.LOGS_DIR", tmp_path):
            srv = DashboardServer(port=port)
            srv.start(blocking=False)
            time.sleep(0.3)
            yield srv, port, tmp_path
            srv.stop()

    def _get(self, port, path):
        url = f"http://localhost:{port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_status_has_populated_data(self, server_with_data):
        srv, port, _ = server_with_data
        status, data = self._get(port, "/api/status")
        assert data["trade_count"] == 10
        assert data["wins"] == 7

    def test_candles_returns_populated(self, server_with_data):
        srv, port, _ = server_with_data
        status, data = self._get(port, "/api/candles")
        assert len(data) == 2
        assert data[0]["o"] == 24500

    def test_decisions_returns_populated(self, server_with_data):
        srv, port, _ = server_with_data
        status, data = self._get(port, "/api/decisions")
        assert len(data) == 2
        assert data[0]["decision"] == "APPROVED"

    def test_safety_returns_populated(self, server_with_data):
        srv, port, _ = server_with_data
        status, data = self._get(port, "/api/safety")
        assert data["daily_pnl"] == -100.0
        assert data["all_ok"] is True
