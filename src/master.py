"""Master MT5 terminal polling and change detection."""

from __future__ import annotations

import logging
import time
from typing import Optional

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    # EA-only master mode: the bridge tails TradeSender.mq5's signal file and
    # never touches MT5 IPC, so the package is optional at import time. The
    # IPC methods below fail closed (return False / []) when it is absent.
    mt5 = None
    _MT5_AVAILABLE = False

from src.config import MasterConfig
from src.models import Position, PendingOrder, TradeEvent

logger = logging.getLogger(__name__)


class MasterMonitor:
    """Polls the master MT5 terminal and detects position changes."""

    def __init__(self, config: MasterConfig):
        self._cfg = config
        self._snapshot: dict[int, Position] = {}  # ticket -> Position
        self._known_tickets: set[int] = set()      # tickets we've copied
        self._order_snapshot: dict[int, PendingOrder] = {}
        self._known_order_tickets: set[int] = set()
        self._connected = False

    @property
    def known_tickets(self) -> set[int]:
        return self._known_tickets

    def ensure_connected(self) -> bool:
        """Make sure the IPC connection to the master terminal is alive.

        ``mt5.initialize()`` / ``mt5.shutdown()`` spawn and tear down the
        terminal pipe handshake, so doing it every poll cycle dominated the
        bridge loop. This keeps the connection open across cycles and only
        re-initializes after a poll detected the terminal went away (see
        ``_mark_disconnected``).
        """
        if self._connected:
            return True
        return self.connect()

    def connect(self) -> bool:
        """(Re)initialize the connection to master terminal."""
        if not _MT5_AVAILABLE:
            logger.error(
                "Master connect failed: MetaTrader5 package not installed — "
                "use master.ea_signals_file (EA mode) instead of IPC"
            )
            return False
        try:
            result = mt5.initialize(path=self._cfg.path, port=self._cfg.port)
        except RuntimeError as e:
            logger.error("Master connect failed: %s", e)
            result = False
        self._connected = bool(result)
        if not result:
            logger.error("Master connect failed: %s", mt5.last_error())
        return self._connected

    def _mark_disconnected(self) -> None:
        """Note that the IPC connection died; next tick re-initializes."""
        self._connected = False

    def disconnect(self) -> None:
        if not _MT5_AVAILABLE:
            return
        try:
            mt5.shutdown()
        except RuntimeError:
            pass
        self._connected = False

    def poll(self) -> list[Position]:
        """Fetch all open positions from master. Returns empty list on error."""
        try:
            positions = mt5.positions_get()
        except RuntimeError as e:
            logger.warning("Master positions_get raised: %s", e)
            self._mark_disconnected()
            return []
        if positions is None:
            logger.warning("Master positions_get returned None: %s", mt5.last_error())
            self._mark_disconnected()
            return []
        return [Position.from_mt5(p) for p in positions]

    def poll_orders(self) -> list[PendingOrder]:
        """Fetch all pending orders from master. Returns empty list on error."""
        try:
            orders = mt5.orders_get()
        except RuntimeError as e:
            logger.warning("Master orders_get raised: %s", e)
            self._mark_disconnected()
            return []
        if orders is None:
            logger.warning("Master orders_get returned None: %s", mt5.last_error())
            self._mark_disconnected()
            return []
        return [PendingOrder.from_mt5(o) for o in orders]

    def snapshot(self, positions: list[Position], orders: list[PendingOrder] | None = None) -> None:
        """Set initial snapshot — positions and orders that existed before bridge started."""
        self._snapshot = {p.ticket: p for p in positions}
        logger.info("Master snapshot: %d positions (will not auto-copy)", len(self._snapshot))
        if orders is not None:
            self._order_snapshot = {o.ticket: o for o in orders}
            logger.info("Master order snapshot: %d pending orders", len(self._order_snapshot))

    def detect_changes(self, current: list[Position]) -> list[TradeEvent]:
        """
        Compare current positions with snapshot to detect open/close/modify events.
        Ignores positions that existed in the initial snapshot.
        """
        events: list[TradeEvent] = []
        curr_by_ticket = {p.ticket: p for p in current}
        prev_by_ticket = dict(self._snapshot)

        # --- DETECT NEW POSITIONS (OPEN) ---
        for ticket, pos in curr_by_ticket.items():
            if ticket not in prev_by_ticket:
                # Was it already known from a prior cycle? (duplicate detection)
                if ticket not in self._known_tickets:
                    events.append(TradeEvent(
                        action="open",
                        symbol=pos.symbol,
                        volume=pos.volume,
                        price=pos.price_open,
                        sl=pos.sl if pos.sl else None,
                        tp=pos.tp if pos.tp else None,
                        master_ticket=ticket,
                        position_type=pos.type,
                        comment=pos.comment,
                        magic=pos.magic,
                    ))
                    self._known_tickets.add(ticket)

        # --- DETECT CLOSED POSITIONS (CLOSE) ---
        for ticket, pos in prev_by_ticket.items():
            if ticket not in curr_by_ticket:
                if ticket in self._known_tickets:
                    events.append(TradeEvent(
                        action="close",
                        symbol=pos.symbol,
                        volume=pos.volume,
                        price=pos.price_current,
                        sl=pos.sl if pos.sl else None,
                        tp=pos.tp if pos.tp else None,
                        master_ticket=ticket,
                        position_type=pos.type,
                        comment=pos.comment,
                        magic=pos.magic,
                    ))
                    self._known_tickets.discard(ticket)

        # --- DETECT MODIFIED POSITIONS (MODIFY) ---
        for ticket, curr_pos in curr_by_ticket.items():
            if ticket in prev_by_ticket and ticket in self._known_tickets:
                prev_pos = prev_by_ticket[ticket]
                sl_changed = (curr_pos.sl != prev_pos.sl)
                tp_changed = (curr_pos.tp != prev_pos.tp)
                vol_changed = (curr_pos.volume != prev_pos.volume)

                if sl_changed or tp_changed or vol_changed:
                    events.append(TradeEvent(
                        action="modify",
                        symbol=curr_pos.symbol,
                        volume=curr_pos.volume,
                        price=curr_pos.price_open,
                        sl=curr_pos.sl if curr_pos.sl else None,
                        tp=curr_pos.tp if curr_pos.tp else None,
                        master_ticket=ticket,
                        position_type=curr_pos.type,
                        comment=curr_pos.comment,
                        magic=curr_pos.magic,
                        prev_volume=prev_pos.volume if vol_changed else None,
                    ))

        self._snapshot = curr_by_ticket
        return events

    def detect_order_changes(self, current: list[PendingOrder]) -> list[TradeEvent]:
        """Detect new, removed, or modified pending orders."""
        events: list[TradeEvent] = []
        curr_by_ticket = {o.ticket: o for o in current}
        prev_by_ticket = dict(self._order_snapshot)

        # --- NEW PENDING ORDERS (place) ---
        for ticket, order in curr_by_ticket.items():
            if ticket not in prev_by_ticket:
                if ticket not in self._known_order_tickets:
                    events.append(TradeEvent(
                        action="place",
                        symbol=order.symbol,
                        volume=order.volume,
                        price=order.price,
                        sl=order.sl if order.sl else None,
                        tp=order.tp if order.tp else None,
                        master_ticket=ticket,
                        position_type=order.type,
                        comment=order.comment,
                        magic=order.magic,
                        order_type=order.type,
                        expiration=order.expiration if order.expiration else None,
                    ))
                    self._known_order_tickets.add(ticket)

        # --- REMOVED PENDING ORDERS (delete) ---
        for ticket, order in prev_by_ticket.items():
            if ticket not in curr_by_ticket:
                if ticket in self._known_order_tickets:
                    events.append(TradeEvent(
                        action="delete",
                        symbol=order.symbol,
                        volume=order.volume,
                        price=order.price,
                        sl=order.sl if order.sl else None,
                        tp=order.tp if order.tp else None,
                        master_ticket=ticket,
                        position_type=order.type,
                        comment=order.comment,
                        magic=order.magic,
                        order_type=order.type,
                    ))
                    self._known_order_tickets.discard(ticket)

        # --- MODIFIED PENDING ORDERS (modify_order) ---
        for ticket, curr in curr_by_ticket.items():
            if ticket in prev_by_ticket and ticket in self._known_order_tickets:
                prev = prev_by_ticket[ticket]
                changed = (
                    curr.price != prev.price or
                    curr.sl != prev.sl or
                    curr.tp != prev.tp or
                    curr.volume != prev.volume or
                    curr.expiration != prev.expiration
                )
                if changed:
                    events.append(TradeEvent(
                        action="modify_order",
                        symbol=curr.symbol,
                        volume=curr.volume,
                        price=curr.price,
                        sl=curr.sl if curr.sl else None,
                        tp=curr.tp if curr.tp else None,
                        master_ticket=ticket,
                        position_type=curr.type,
                        comment=curr.comment,
                        magic=curr.magic,
                        order_type=curr.type,
                        expiration=curr.expiration if curr.expiration else None,
                    ))

        self._order_snapshot = curr_by_ticket
        return events

    def run_once(self) -> list[TradeEvent]:
        """Full poll-detect cycle. Returns events to execute.

        Uses the persistent connection — a dead terminal is detected by the
        polls and re-initialized on the next cycle.
        """
        if not self.ensure_connected():
            logger.warning("Skipping master poll cycle — connection failed")
            time.sleep(1.0)
            return []

        positions = self.poll()
        orders = self.poll_orders()
        events = self.detect_changes(positions)
        events += self.detect_order_changes(orders)
        return events
