"""
IBKR Position Manager
======================
Tracks open positions, reconciles against broker state, and
feeds realized P&L to the order executor for daily-loss-limit
enforcement.

Responsibilities:
  1. Internal position ledger (entry price, size, side, tags, order IDs)
  2. Reconciliation loop: every 30s, query IBKR portfolio and compare
  3. Mismatch → CRITICAL log + HALT (no auto-correction, human intervenes)
  4. Realized P&L per trade and cumulative daily P&L
  5. Partial fill tracking (C1 fills but C2 doesn't)
  6. Immediate state update on position close (no waiting for recon cycle)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from Broker.ibkr_client import IBKRClient
from Broker.order_executor import IBKROrderExecutor, MNQ_POINT_VALUE

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
RECONCILIATION_INTERVAL_SECONDS = 30
COMMISSION_PER_CONTRACT = 1.29   # matches RiskConfig.commission_per_contract


# ═══════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════

class FillState(Enum):
    UNFILLED = "unfilled"
    FILLED = "filled"
    PARTIAL = "partial"          # scale-out: one leg filled, other not


class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"


# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class TrackedPosition:
    """A single tracked position with full audit trail."""
    position_id: str
    broker_order_id: str
    side: PositionSide
    contracts: int
    entry_price: float
    entry_time: datetime
    tag: str = ""                # "C1", "C2"

    # Fill state
    fill_state: FillState = FillState.FILLED

    # Exit info (populated on close)
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    is_open: bool = True

    # P&L
    gross_pnl: float = 0.0
    commission: float = COMMISSION_PER_CONTRACT
    net_pnl: float = 0.0


@dataclass
class ScaleOutGroup:
    """Groups C1 and C2 legs of a single trade entry."""
    group_id: str
    direction: str               # "long" or "short"
    c1: Optional[TrackedPosition] = None
    c2: Optional[TrackedPosition] = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def is_partial(self) -> bool:
        """True if only one leg filled."""
        c1_filled = self.c1 is not None and self.c1.fill_state == FillState.FILLED
        c2_filled = self.c2 is not None and self.c2.fill_state == FillState.FILLED
        return c1_filled != c2_filled

    @property
    def is_fully_closed(self) -> bool:
        c1_closed = self.c1 is None or not self.c1.is_open
        c2_closed = self.c2 is None or not self.c2.is_open
        return c1_closed and c2_closed

    @property
    def total_net_pnl(self) -> float:
        pnl = 0.0
        if self.c1:
            pnl += self.c1.net_pnl
        if self.c2:
            pnl += self.c2.net_pnl
        return round(pnl, 2)


@dataclass
class ReconciliationResult:
    """Outcome of a single reconciliation check."""
    timestamp: datetime
    matched: bool
    internal_count: int
    broker_count: int
    ghost_positions: List[dict] = field(default_factory=list)
    missing_positions: List[str] = field(default_factory=list)
    details: str = ""


@dataclass
class BrokerPosition:
    """Parsed position from IBKR portfolio endpoint."""
    conid: int
    symbol: str
    quantity: int                 # signed: positive=long, negative=short
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


# ═══════════════════════════════════════════════════════════════
# POSITION MANAGER
# ═══════════════════════════════════════════════════════════════

class PositionManager:
    """
    Tracks positions, reconciles with IBKR, and feeds P&L
    to the order executor.

    Reconciliation mismatches trigger HALT — no auto-correction.
    Human must intervene.
    """

    def __init__(
        self,
        client: IBKRClient,
        executor: IBKROrderExecutor,
    ):
        self._client = client
        self._executor = executor

        # Position ledger
        self._open_positions: Dict[str, TrackedPosition] = {}
        self._closed_positions: List[TrackedPosition] = []
        self._scale_out_groups: Dict[str, ScaleOutGroup] = {}

        # Daily P&L
        self._daily_realized_pnl: float = 0.0
        self._trade_count: int = 0

        # Reconciliation
        self._recon_task: Optional[asyncio.Task] = None
        self._last_recon: Optional[ReconciliationResult] = None
        self._recon_history: List[ReconciliationResult] = []

    # ──────────────────────────────────────────────────────────
    # POSITION TRACKING
    # ──────────────────────────────────────────────────────────

    def open_position(
        self,
        position_id: str,
        broker_order_id: str,
        side: str,
        contracts: int,
        entry_price: float,
        tag: str = "",
        group_id: str = "",
    ) -> TrackedPosition:
        """
        Register a new open position.  Called immediately on fill.
        """
        pos = TrackedPosition(
            position_id=position_id,
            broker_order_id=broker_order_id,
            side=PositionSide.LONG if side == "long" else PositionSide.SHORT,
            contracts=contracts,
            entry_price=round(entry_price, 2),
            entry_time=datetime.now(timezone.utc),
            tag=tag,
        )
        self._open_positions[position_id] = pos

        # Wire into scale-out group if provided
        if group_id:
            group = self._scale_out_groups.get(group_id)
            if not group:
                group = ScaleOutGroup(
                    group_id=group_id,
                    direction=side,
                )
                self._scale_out_groups[group_id] = group

            if tag == "C1":
                group.c1 = pos
            elif tag == "C2":
                group.c2 = pos

            # Check for partial fill
            if group.is_partial:
                unfilled_tag = "C2" if tag == "C1" else "C1"
                logger.warning(
                    "PARTIAL FILL: group=%s — %s filled but %s did not",
                    group_id, tag, unfilled_tag,
                )

        logger.info(
            "POSITION OPENED: id=%s side=%s contracts=%d "
            "entry=%.2f tag=%s group=%s",
            position_id, side, contracts,
            entry_price, tag, group_id or "—",
        )
        return pos

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_reason: str = "",
    ) -> Optional[TrackedPosition]:
        """
        Close a position immediately and compute realized P&L.

        Updates internal state right away — does NOT wait for
        the reconciliation cycle.
        """
        pos = self._open_positions.pop(position_id, None)
        if pos is None:
            logger.warning(
                "close_position called for unknown id=%s", position_id
            )
            return None

        pos.exit_price = round(exit_price, 2)
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = exit_reason
        pos.is_open = False

        # Compute P&L
        pos.gross_pnl = self._compute_pnl(
            pos.side, pos.entry_price, pos.exit_price, pos.contracts
        )
        pos.net_pnl = round(pos.gross_pnl - pos.commission, 2)

        self._closed_positions.append(pos)
        self._daily_realized_pnl = round(
            self._daily_realized_pnl + pos.net_pnl, 2
        )
        self._trade_count += 1

        # Feed P&L to executor for daily loss limit / kill switch
        self._executor.record_trade_pnl(pos.net_pnl)

        # Also remove from executor's position ledger
        self._executor.close_position(pos.broker_order_id)

        logger.info(
            "POSITION CLOSED: id=%s side=%s entry=%.2f exit=%.2f "
            "reason=%s gross=%.2f net=%.2f daily_total=%.2f",
            position_id, pos.side.value, pos.entry_price,
            pos.exit_price, exit_reason,
            pos.gross_pnl, pos.net_pnl, self._daily_realized_pnl,
        )
        return pos

    def mark_partial_fill(
        self, group_id: str, unfilled_tag: str
    ) -> None:
        """
        Mark a leg of a scale-out group as unfilled.
        Called when one leg fills but the other does not.
        """
        group = self._scale_out_groups.get(group_id)
        if not group:
            logger.warning(
                "mark_partial_fill: unknown group=%s", group_id
            )
            return

        if unfilled_tag == "C1" and group.c1:
            group.c1.fill_state = FillState.UNFILLED
        elif unfilled_tag == "C2" and group.c2:
            group.c2.fill_state = FillState.UNFILLED

        logger.warning(
            "PARTIAL FILL RECORDED: group=%s — %s marked unfilled",
            group_id, unfilled_tag,
        )

    # ──────────────────────────────────────────────────────────
    # P&L CALCULATION
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _compute_pnl(
        side: PositionSide,
        entry_price: float,
        exit_price: float,
        contracts: int,
    ) -> float:
        """
        Compute gross P&L in dollars.

        Uses MNQ_POINT_VALUE ($2.00/point) from order_executor
        to stay consistent with the rest of the system.
        """
        if side == PositionSide.LONG:
            points = exit_price - entry_price
        else:
            points = entry_price - exit_price

        return round(points * MNQ_POINT_VALUE * contracts, 2)

    def get_unrealized_pnl(self, current_price: float) -> float:
        """Compute total unrealized P&L across all open positions."""
        total = 0.0
        for pos in self._open_positions.values():
            total += self._compute_pnl(
                pos.side, pos.entry_price, current_price, pos.contracts
            )
        return round(total, 2)

    # ──────────────────────────────────────────────────────────
    # RECONCILIATION
    # ──────────────────────────────────────────────────────────

    async def start_reconciliation_loop(self) -> None:
        """Start the background reconciliation loop (every 30s)."""
        if self._recon_task and not self._recon_task.done():
            logger.warning("Reconciliation loop already running")
            return
        self._recon_task = asyncio.create_task(self._reconciliation_loop())
        logger.info(
            "Reconciliation loop started (interval=%ds)",
            RECONCILIATION_INTERVAL_SECONDS,
        )

    async def stop_reconciliation_loop(self) -> None:
        """Stop the background reconciliation loop."""
        if self._recon_task and not self._recon_task.done():
            self._recon_task.cancel()
            try:
                await self._recon_task
            except asyncio.CancelledError:
                pass
            self._recon_task = None
            logger.info("Reconciliation loop stopped")

    async def _reconciliation_loop(self) -> None:
        """Run reconciliation on a fixed interval."""
        try:
            while True:
                await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)
                await self.reconcile()
        except asyncio.CancelledError:
            return

    async def reconcile(self) -> ReconciliationResult:
        """
        Query IBKR for actual positions and compare against
        internal state.

        On mismatch:
          - Log CRITICAL with full details
          - HALT the system via order_executor
          - Do NOT attempt auto-correction
        """
        broker_positions = await self._fetch_broker_positions()
        result = self._compare_positions(broker_positions)

        self._last_recon = result
        self._recon_history.append(result)

        if not result.matched:
            logger.critical(
                "RECONCILIATION MISMATCH: internal=%d broker=%d "
                "ghosts=%d missing=%d — %s",
                result.internal_count,
                result.broker_count,
                len(result.ghost_positions),
                len(result.missing_positions),
                result.details,
            )
            # HALT — human must intervene
            await self._executor.emergency_flatten(
                f"Position reconciliation mismatch: {result.details}"
            )
        else:
            logger.debug(
                "Reconciliation OK: %d positions match",
                result.internal_count,
            )

        return result

    async def _fetch_broker_positions(self) -> List[BrokerPosition]:
        """
        Fetch open positions from IBKR Client Portal Gateway.

        Endpoint: GET /portfolio/{accountId}/positions/0
        Returns list of position dicts with conid, position, avgPrice, etc.
        """
        account_id = self._client.account_id
        if not account_id:
            logger.warning("No account_id — cannot fetch positions")
            return []

        endpoint = f"/portfolio/{account_id}/positions/0"
        data = await self._client._get(endpoint)

        if data is None:
            logger.warning("Failed to fetch broker positions")
            return []

        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []

        positions = []
        for item in data:
            conid = item.get("conid", 0)
            # Only track our contract
            if (self._client.contract and
                    conid != self._client.contract.conid):
                continue
            positions.append(BrokerPosition(
                conid=conid,
                symbol=item.get("contractDesc", ""),
                quantity=int(item.get("position", 0)),
                avg_price=round(float(item.get("avgPrice", 0.0)), 2),
                unrealized_pnl=round(
                    float(item.get("unrealizedPnl", 0.0)), 2
                ),
                realized_pnl=round(
                    float(item.get("realizedPnl", 0.0)), 2
                ),
            ))

        return positions

    def _compare_positions(
        self, broker_positions: List[BrokerPosition]
    ) -> ReconciliationResult:
        """
        Compare internal position ledger against broker reality.

        A mismatch is either:
          - Ghost position: broker shows position we don't track
          - Missing position: we track a position broker doesn't show
        """
        now = datetime.now(timezone.utc)

        # Internal net quantity (signed: positive=long, negative=short)
        internal_net = 0
        for pos in self._open_positions.values():
            if pos.side == PositionSide.LONG:
                internal_net += pos.contracts
            else:
                internal_net -= pos.contracts

        # Broker net quantity
        broker_net = 0
        for bp in broker_positions:
            broker_net += bp.quantity

        internal_count = len(self._open_positions)
        broker_count = len(broker_positions)
        ghost_positions: List[dict] = []
        missing_positions: List[str] = []

        if internal_net != broker_net:
            # Determine what's wrong
            if broker_net != 0 and internal_count == 0:
                ghost_positions.append({
                    "type": "ghost",
                    "broker_quantity": broker_net,
                    "internal_quantity": internal_net,
                })
            elif broker_net == 0 and internal_count > 0:
                missing_positions = list(self._open_positions.keys())
            else:
                ghost_positions.append({
                    "type": "quantity_mismatch",
                    "broker_quantity": broker_net,
                    "internal_quantity": internal_net,
                })

            details = (
                f"net qty mismatch: internal={internal_net}, "
                f"broker={broker_net}"
            )
            return ReconciliationResult(
                timestamp=now,
                matched=False,
                internal_count=internal_count,
                broker_count=broker_count,
                ghost_positions=ghost_positions,
                missing_positions=missing_positions,
                details=details,
            )

        return ReconciliationResult(
            timestamp=now,
            matched=True,
            internal_count=internal_count,
            broker_count=broker_count,
            details="positions match",
        )

    # ──────────────────────────────────────────────────────────
    # PROPERTIES & STATUS
    # ──────────────────────────────────────────────────────────

    @property
    def open_positions(self) -> Dict[str, TrackedPosition]:
        return dict(self._open_positions)

    @property
    def open_position_count(self) -> int:
        return len(self._open_positions)

    @property
    def daily_realized_pnl(self) -> float:
        return self._daily_realized_pnl

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def last_reconciliation(self) -> Optional[ReconciliationResult]:
        return self._last_recon

    def get_scale_out_group(
        self, group_id: str
    ) -> Optional[ScaleOutGroup]:
        return self._scale_out_groups.get(group_id)

    def get_status(self) -> dict:
        """Return position manager health snapshot."""
        return {
            "open_positions": self.open_position_count,
            "daily_realized_pnl": self._daily_realized_pnl,
            "trade_count": self._trade_count,
            "closed_positions": len(self._closed_positions),
            "scale_out_groups": len(self._scale_out_groups),
            "last_recon_matched": (
                self._last_recon.matched if self._last_recon else None
            ),
            "last_recon_time": (
                self._last_recon.timestamp.isoformat()
                if self._last_recon else None
            ),
            "recon_loop_active": (
                self._recon_task is not None
                and not self._recon_task.done()
                if self._recon_task else False
            ),
        }

    def reset_daily(self) -> None:
        """Reset daily counters at session open."""
        self._daily_realized_pnl = 0.0
        self._trade_count = 0
        self._closed_positions.clear()
        self._scale_out_groups.clear()
        logger.info("Position manager daily reset complete")
