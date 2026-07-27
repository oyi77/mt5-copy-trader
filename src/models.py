"""Data models for copy trade events and position tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Position:
    """Simplified position from MT5."""
    ticket: int
    symbol: str
    volume: float
    price_open: float
    price_current: float
    profit: float
    sl: float
    tp: float
    type: int  # 0=POSITION_TYPE_BUY, 1=POSITION_TYPE_SELL
    comment: str
    magic: int

    @classmethod
    def from_mt5(cls, pos: Any) -> Position:
        return cls(
            ticket=pos.ticket,
            symbol=pos.symbol,
            volume=pos.volume,
            price_open=pos.price_open,
            price_current=pos.price_current,
            profit=getattr(pos, 'profit', 0.0),
            sl=pos.sl,
            tp=pos.tp,
            type=pos.type,
            comment=pos.comment,
            magic=pos.magic,
        )


@dataclass
class PendingOrder:
    """A pending order (limit/stop) from MT5."""
    ticket: int
    symbol: str
    volume: float
    price: float
    sl: float
    tp: float
    type: int  # ORDER_TYPE_BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP
    comment: str
    magic: int
    expiration: Optional[int] = None

    @classmethod
    def from_mt5(cls, order: Any) -> PendingOrder:
        return cls(
            ticket=order.ticket,
            symbol=order.symbol,
            volume=order.volume_current,
            price=order.price_open,
            sl=order.sl,
            tp=order.tp,
            type=order.type,
            comment=order.comment,
            magic=order.magic,
            expiration=getattr(order, 'expiration', 0),
        )


@dataclass
class TradeEvent:
    """A detected change on the master that needs to be replicated."""
    action: str  # 'open' | 'close' | 'modify' | 'place' | 'modify_order' | 'delete'
    symbol: str
    volume: float
    price: float
    sl: Optional[float]
    tp: Optional[float]
    master_ticket: int
    position_type: int
    comment: str
    magic: int
    prev_volume: Optional[float] = None
    order_type: Optional[int] = None  # ORDER_TYPE for pending order actions
    expiration: Optional[int] = None

    def volume_change(self) -> Optional[float]:
        """Return the volume to close on partial close, or None."""
        if self.prev_volume is not None and self.volume < self.prev_volume:
            return round(self.prev_volume - self.volume, 2)
        return None


# Magic number range reserved for this bridge
BRIDGE_MAGIC_BASE = 951_000
