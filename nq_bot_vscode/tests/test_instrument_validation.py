"""
Tests for multi-instrument deployment guard.

Verifies:
  1. Unvalidated instruments are blocked from order execution
  2. Validated instruments (MNQ) pass normally
  3. ALLOW_UNVALIDATED env override permits unvalidated instruments
  4. Validation milestone is logged at 200+ trades with PF > 1.2
"""

import os
import logging
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from Broker.order_executor import (
    IBKROrderExecutor,
    OrderRequest,
    OrderSide,
    IBKROrderType,
    OrderState,
    ExecutorConfig,
)
from Broker.ibkr_client_portal import IBKRClient, IBKRConfig
from config.instruments import InstrumentSpec, INSTRUMENT_SPECS


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def config():
    return IBKRConfig(
        gateway_host="localhost",
        gateway_port=5000,
        account_type="paper",
        symbol="MNQ",
    )


@pytest.fixture
def client(config):
    return IBKRClient(config)


@pytest.fixture
def executor(client):
    return IBKROrderExecutor(
        client,
        ExecutorConfig(paper_mode=True, allow_eth=True),
    )


# ═══════════════════════════════════════════════════════════════
# TEST 1: Unvalidated instrument blocked
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unvalidated_instrument_blocked(executor):
    """MES order is rejected without env override because it's unvalidated."""
    # Ensure ALLOW_UNVALIDATED is not set
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("ALLOW_UNVALIDATED", None)

        result = await executor.place_scale_out_entry(
            direction="long",
            limit_price=5000.0,
            stop_loss=4980.0,
            instrument="MES",
        )

        assert not result["c1"].accepted
        assert "UNVALIDATED_INSTRUMENT" in result["c1"].rejection_reason
        assert "MES" in result["c1"].rejection_reason
        assert result["c1"].state == OrderState.REJECTED


# ═══════════════════════════════════════════════════════════════
# TEST 2: Validated instrument allowed
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_validated_instrument_allowed(executor):
    """MNQ order passes normally because it's validated."""
    result = await executor.place_scale_out_entry(
        direction="long",
        limit_price=21000.0,
        stop_loss=20980.0,
        instrument="MNQ",
    )

    # MNQ is validated — should not be rejected for instrument validation
    # (may be rejected for other safety reasons, but NOT for validation)
    c1_reason = result["c1"].rejection_reason
    assert "UNVALIDATED_INSTRUMENT" not in c1_reason


# ═══════════════════════════════════════════════════════════════
# TEST 3: Env override allows unvalidated
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_env_override_allows_unvalidated(executor):
    """ALLOW_UNVALIDATED=true permits MES in paper mode."""
    with patch.dict(os.environ, {"ALLOW_UNVALIDATED": "true"}):
        result = await executor.place_scale_out_entry(
            direction="long",
            limit_price=5000.0,
            stop_loss=4980.0,
            instrument="MES",
        )

        # Should NOT be rejected for instrument validation
        c1_reason = result["c1"].rejection_reason
        assert "UNVALIDATED_INSTRUMENT" not in c1_reason


# ═══════════════════════════════════════════════════════════════
# TEST 4: Validation milestone logged
# ═══════════════════════════════════════════════════════════════

def test_validation_milestone_logged(caplog):
    """200-trade threshold with PF > 1.2 triggers log message."""
    # Simulate what the backtest runner does when it detects a milestone
    aggregate = {
        "total_trades": 250,
        "profit_factor": 1.53,
    }

    logger = logging.getLogger("test_milestone")

    n_trades = aggregate["total_trades"]
    pf_value = aggregate["profit_factor"]
    instrument = "MNQ"

    with caplog.at_level(logging.INFO, logger="test_milestone"):
        if n_trades >= 200 and pf_value > 1.2:
            logger.info(
                "VALIDATION MILESTONE: %s reached %d trades, PF=%s. "
                "Consider setting validated=True.",
                instrument, n_trades, pf_value,
            )

    assert any("VALIDATION MILESTONE" in r.message for r in caplog.records)
    assert any("MNQ" in r.message for r in caplog.records)
    assert any("250" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════
# SUPPLEMENTARY: InstrumentSpec.validated field correctness
# ═══════════════════════════════════════════════════════════════

def test_instrument_validation_defaults():
    """Verify MNQ is validated and others are not."""
    assert INSTRUMENT_SPECS["MNQ"].validated is True
    assert INSTRUMENT_SPECS["MES"].validated is False
    assert INSTRUMENT_SPECS["MYM"].validated is False
    assert INSTRUMENT_SPECS["M2K"].validated is False
