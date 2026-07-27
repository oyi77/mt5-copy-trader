"""Follower MT5 terminal trade execution."""

from __future__ import annotations

import logging
from typing import Optional

import MetaTrader5 as mt5

from src.config import FollowerConfig
from src.models import TradeEvent

logger = logging.getLogger(__name__)


class FollowerExecutor:
    """Connects to a follower MT5 terminal and executes trade events."""

    def __init__(self, config: FollowerConfig):
        self._cfg = config
        self._name = config.name

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, event: TradeEvent) -> bool:
        """Execute a single trade event on this follower.

        Returns True if the operation was submitted successfully.
        """
        symbol = self._map_symbol(event.symbol)
        volume = self._apply_lot_scaling(event.volume)

        if event.action == "open":
            return self._open(symbol, volume, event)
        elif event.action == "close":
            return self._close(symbol, event)
        elif event.action == "modify":
            return self._modify(symbol, event, volume)
        else:
            logger.error("%s: Unknown action %s", self._name, event.action)
            return False

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        result = mt5.initialize(
            path=self._cfg.path,
            port=self._cfg.port,
            login=self._cfg.login,
            password=self._cfg.password,
            server=self._cfg.server,
        )
        if not result:
            logger.error("%s: connect failed: %s", self._name, mt5.last_error())
        else:
            logger.info("%s: connected successfully", self._name)
        return result

    def disconnect(self) -> None:
        mt5.shutdown()

    # ------------------------------------------------------------------
    # Trade actions
    # ------------------------------------------------------------------

    def _open(self, symbol: str, volume: float, event: TradeEvent) -> bool:
        order_type = mt5.ORDER_TYPE_BUY if event.position_type == 0 else mt5.ORDER_TYPE_SELL
        price = self._get_price(symbol, order_type)
        if price is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": self._map_price(event.sl) if event.sl else 0.0,
            "tp": self._map_price(event.tp) if event.tp else 0.0,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": str(event.master_ticket),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: OPEN %s %.2f %s (ticket=%d, comment=%s)",
                self._name, symbol, volume,
                "BUY" if event.position_type == 0 else "SELL",
                result.order, event.master_ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            comment = result.comment if result else ""
            logger.error(
                "%s: OPEN failed — retcode=%d comment=%s request=%s",
                self._name, retcode, comment, self._mask_request(request),
            )
            return False

    def _close(self, symbol: str, event: TradeEvent) -> bool:
        # Find follower position that matches this master ticket
        fpos = self._find_position_by_comment(str(event.master_ticket))
        if fpos is None:
            logger.warning(
                "%s: CLOSE — no follower position found for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        close_type = mt5.ORDER_TYPE_SELL if fpos.type == 0 else mt5.ORDER_TYPE_BUY
        price = self._get_price(symbol, close_type)
        if price is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": fpos.ticket,
            "volume": fpos.volume,
            "type": close_type,
            "price": price,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": f"close_{event.master_ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: CLOSE %s %.2f (master_ticket=%d, f_ticket=%d)",
                self._name, symbol, fpos.volume,
                event.master_ticket, fpos.ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            comment = result.comment if result else ""
            logger.error(
                "%s: CLOSE failed — retcode=%d comment=%s",
                self._name, retcode, comment,
            )
            return False

    def _modify(self, symbol: str, event: TradeEvent, current_volume: float) -> bool:
        fpos = self._find_position_by_comment(str(event.master_ticket))
        if fpos is None:
            logger.warning(
                "%s: MODIFY — no follower position for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        # 1) Partial close if volume decreased
        vol_change = event.volume_change()
        if vol_change is not None:
            self._partial_close(symbol, fpos, vol_change, event)
            # Re-fetch position after partial close (volume changed)
            fpos = self._find_position_by_comment(str(event.master_ticket))
            if fpos is None:
                logger.warning(
                    "%s: MODIFY — position vanished after partial close",
                    self._name,
                )
                return False

        # 2) Update SL/TP
        has_sl = event.sl is not None
        has_tp = event.tp is not None

        if has_sl or has_tp:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": fpos.ticket,
                "sl": event.sl if has_sl else fpos.sl,
                "tp": event.tp if has_tp else fpos.tp,
                "magic": self._cfg.magic,
                "comment": f"mod_{event.master_ticket}",
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    "%s: MODIFY SL/TP %s (sl=%s tp=%s, master_ticket=%d)",
                    self._name, symbol,
                    event.sl, event.tp,
                    event.master_ticket,
                )
            else:
                retcode = result.retcode if result else -1
                logger.error(
                    "%s: MODIFY SL/TP failed — retcode=%d",
                    self._name, retcode,
                )

        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _partial_close(
        self, symbol: str, fpos, close_volume: float, event: TradeEvent,
    ) -> bool:
        close_type = mt5.ORDER_TYPE_SELL if fpos.type == 0 else mt5.ORDER_TYPE_BUY
        price = self._get_price(symbol, close_type)
        if price is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": fpos.ticket,
            "volume": close_volume,
            "type": close_type,
            "price": price,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": f"pclose_{event.master_ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: PARTIAL CLOSE %s %.2f (master_ticket=%d)",
                self._name, symbol, close_volume, event.master_ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            logger.error(
                "%s: PARTIAL CLOSE failed — retcode=%d",
                self._name, retcode,
            )
            return False

    def _find_position_by_comment(self, comment: str):
        """Find an open position with the given comment string."""
        positions = mt5.positions_get()
        if not positions:
            return None
        for p in positions:
            if p.comment == comment:
                return p
        return None

    def _map_symbol(self, symbol: str) -> str:
        mapping = self._cfg.symbol_mapping
        upper = symbol.upper()
        return mapping.get(upper, upper)

    def _apply_lot_scaling(self, volume: float) -> float:
        vol = volume * self._cfg.lot_multiplier
        vol = max(vol, self._cfg.min_lot)
        vol = min(vol, self._cfg.max_lot)
        # Round to 2 decimals (standard lot step)
        return round(vol, 2)

    def _map_price(self, price: float) -> float:
        """Round to 5 decimals (standard forex precision)."""
        return round(price, 5)

    def _get_price(self, symbol: str, order_type: int) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("%s: symbol_info_tick failed for %s", self._name, symbol)
            return None
        return tick.ask if order_type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP) else tick.bid

    def _mask_request(self, req: dict) -> dict:
        """Remove sensitive fields for logging."""
        masked = dict(req)
        masked.pop("password", None)
        return masked
