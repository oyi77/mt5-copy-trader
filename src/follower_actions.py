"""MT5 trade-execution actions for a follower.

This is a mixin (no ``__init__``) — the composing executor provides ``_cfg``,
``_name``, ``_dry_run``, and the ``_map_price`` / ``_apply_lot_scaling`` helpers
from SymbolMappingMixin. Implements the actual order_send() calls for opening,
closing, modifying, and placing/deleting positions and pending orders, plus the
comment-magic dedup lookups that make stale re-deliveries safe no-ops.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from src.mt5_compat import mt5
from src.models import TradeEvent

logger = logging.getLogger(__name__)


class TradeActionsMixin:
    """Execute trade events against the follower MT5 terminal via IPC."""

    ORDER_TYPE_MAP = {
        2: mt5.ORDER_TYPE_BUY_LIMIT,
        3: mt5.ORDER_TYPE_SELL_LIMIT,
        4: mt5.ORDER_TYPE_BUY_STOP,
        5: mt5.ORDER_TYPE_SELL_STOP,
        6: mt5.ORDER_TYPE_BUY_STOP_LIMIT,
        7: mt5.ORDER_TYPE_SELL_STOP_LIMIT,
    }

    def _open(self, symbol: str, volume: float, event: TradeEvent) -> bool:
        # Replay safety: skip if position with this comment already exists
        existing = self._find_position_by_comment(str(event.master_ticket))
        if existing is not None:
            logger.info("%s: position for ticket %d already exists (sl=%.5f tp=%.5f), skipping",
                        self._name, event.master_ticket, existing.sl, existing.tp)
            return True

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
            "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
            "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": str(event.master_ticket),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self._order_send(request)
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
            if self._history_has_ticket(str(event.master_ticket)):
                # Round trip already completed — this is a stale re-delivery
                # of a close (e.g. hub replay after reconnect). The desired
                # end state already holds; treat it as a silent success.
                logger.info(
                    "%s: CLOSE — ticket %d already closed (round trip complete), "
                    "stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
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

        result = self._order_send(request)
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
            if self._history_has_ticket(str(event.master_ticket)):
                logger.info(
                    "%s: MODIFY — ticket %d already closed (round trip complete), "
                    "stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: MODIFY — no follower position for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        # 1) Partial close if volume decreased
        close_vol = event.volume_change()
        if close_vol:
            # Scale the master-side delta by the follower's lot multiplier
            # (clamped to min/max lot), matching how the open volume is scaled.
            close_vol = self._apply_lot_scaling(close_vol, symbol)
            if not self._partial_close(symbol, fpos, close_vol, event):
                # Abort — proceeding would modify SL/TP on a position that
                # still holds the old volume, silently desyncing sizes.
                logger.warning(
                    "%s: MODIFY — partial close failed, aborting (SL/TP untouched)",
                    self._name,
                )
                return False
            fpos = self._find_position_by_comment(str(event.master_ticket))
            if fpos is None:
                return False

        # 2) SL/TP modification
        if (event.sl is not None and event.sl != fpos.sl) or \
           (event.tp is not None and event.tp != fpos.tp):
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": fpos.ticket,
                "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
                "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
                "magic": self._cfg.magic,
                "comment": fpos.comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = self._order_send(request)
            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                retcode = result.retcode if result else -1
                logger.error(
                    "%s: MODIFY SL/TP failed — retcode=%d",
                    self._name, retcode,
                )
                return False

        logger.info(
            "%s: MODIFY %s %.2f (master_ticket=%d)",
            self._name, symbol, current_volume, event.master_ticket,
        )
        return True

    def _place_order(self, symbol: str, volume: float, event: TradeEvent) -> bool:
        # Replay safety: skip if pending order with this comment already exists
        existing = self._find_order_by_comment(str(event.master_ticket))
        if existing is not None:
            logger.info("%s: pending order for ticket %d already exists, skipping",
                        self._name, event.master_ticket)
            return True

        order_type = self.ORDER_TYPE_MAP.get(event.order_type)
        if order_type is None:
            logger.error(
                "%s: PLACE — unknown order type %s (expected 2-7), aborting",
                self._name, event.order_type,
            )
            return False

        price = event.price
        if price <= 0:
            logger.error("%s: PLACE — invalid price %.5f", self._name, price)
            return False

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
            "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": str(event.master_ticket),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if event.expiration:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = event.expiration

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: PLACE %s %s %.2f @ %.5f (ticket=%d, comment=%s)",
                self._name, symbol,
                [k for k, v in self.ORDER_TYPE_MAP.items() if v == order_type][0],
                volume, price, result.order, event.master_ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            comment = result.comment if result else ""
            logger.error(
                "%s: PLACE failed — retcode=%d comment=%s",
                self._name, retcode, comment,
            )
            return False

    def _modify_order(self, symbol: str, event: TradeEvent) -> bool:
        f_order = self._find_order_by_comment(str(event.master_ticket))
        if f_order is None:
            if self._history_has_ticket(str(event.master_ticket)):
                logger.info(
                    "%s: MODIFY_ORDER — ticket %d already done (round trip "
                    "complete), stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: MODIFY_ORDER — no pending order for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        request = {
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": f_order.ticket,
            "symbol": symbol,
            "price": event.price,
            "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
            "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
            "magic": self._cfg.magic,
            "comment": f_order.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if event.expiration:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = event.expiration

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: MODIFY_ORDER %s (master_ticket=%d, f_ticket=%d)",
                self._name, symbol, event.master_ticket, f_order.ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            logger.error(
                "%s: MODIFY_ORDER failed — retcode=%d",
                self._name, retcode,
            )
            return False

    def _delete_order(self, symbol: str, event: TradeEvent) -> bool:
        f_order = self._find_order_by_comment(str(event.master_ticket))
        if f_order is None:
            if self._history_has_ticket(str(event.master_ticket)):
                logger.info(
                    "%s: DELETE — ticket %d already done (round trip complete), "
                    "stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: DELETE — no pending order for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": f_order.ticket,
            "symbol": symbol,
            "magic": self._cfg.magic,
            "comment": f_order.comment,
        }

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: DELETE %s (master_ticket=%d, f_ticket=%d)",
                self._name, symbol, event.master_ticket, f_order.ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            logger.error(
                "%s: DELETE failed — retcode=%d",
                self._name, retcode,
            )
            return False

    # ------------------------------------------------------------------
    # Dedup / lookup helpers
    # ------------------------------------------------------------------

    def _find_order_by_comment(self, comment: str):
        """Find a pending order matching comment AND magic number."""
        orders = mt5.orders_get()
        if orders is None:
            return None
        for order in orders:
            if order.comment == comment and order.magic == self._cfg.magic:
                return order
        return None

    def _find_position_by_comment(self, comment: str):
        """Find an open position matching comment AND magic number."""
        positions = mt5.positions_get()
        if positions is None:
            return None
        for pos in positions:
            if pos.comment == comment and pos.magic == self._cfg.magic:
                return pos
        return None

    def _history_has_ticket(self, comment: str) -> bool:
        """True if this follower already has deal/order history (7 days) for the
        master ticket (magic + comment match).

        A completed round trip leaves both an entry deal and a close deal in
        MT5 history; a placed pending order leaves its order record even after
        execution or deletion. A history hit therefore means the event was
        materialized in an earlier delivery — used to turn stale re-deliveries
        (hub replay after reconnect, crash-surviving queue entries) into silent
        no-ops instead of re-executing positions the master has long closed.
        """
        try:
            start = datetime.now() - timedelta(days=7)
            end = datetime.now() + timedelta(seconds=1)
            deals = mt5.history_deals_get(start, end)
            if deals:
                for d in deals:
                    if d.comment == comment and d.magic == self._cfg.magic:
                        return True
            orders = mt5.history_orders_get(start, end)
            if orders:
                for o in orders:
                    if o.comment == comment and o.magic == self._cfg.magic:
                        return True
        except Exception as e:
            logger.warning(
                "%s: history dedup check failed (proceeding): %s", self._name, e,
            )
        return False

    # ------------------------------------------------------------------
    # Price / send helpers
    # ------------------------------------------------------------------

    def _get_price(self, symbol: str, order_type: int) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            # Symbol not quoted yet (e.g. freshly switched account, or the
            # symbol is missing from Market Watch). Request a Market Watch
            # subscription and give the feed a moment to populate before
            # deciding there is no price.
            try:
                selected = mt5.symbol_select(symbol, True)
            except Exception as e:
                selected = False
                logger.warning("%s: symbol_select(%s) raised: %s", self._name, symbol, e)
            logger.info(
                "%s: no tick for %s, symbol_select -> %s, waiting for quote...",
                self._name, symbol, selected,
            )
            for _ in range(6):  # up to ~3s
                time.sleep(0.5)
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None:
                    break
        if tick is None:
            logger.error("%s: no tick for %s", self._name, symbol)
            return None
        return tick.ask if order_type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT) else tick.bid

    def _order_send(self, request: dict):
        """Wrap mt5.order_send with DRY_RUN guard.

        When dry_run is True, logs the request and returns a mock
        result with TRADE_RETCODE_DONE (without executing).
        """
        if self._dry_run:
            logger.info(
                "%s: DRY_RUN would send: %s",
                self._name, self._mask_request(request),
            )
            # Return a mock successful order result
            return type(
                "MockResult", (),
                {"retcode": mt5.TRADE_RETCODE_DONE, "order": 0},
            )()
        return mt5.order_send(request)

    def _mask_request(self, req: dict) -> dict:
        """Remove sensitive fields for logging."""
        masked = dict(req)
        masked.pop("password", None)
        return masked

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
        result = self._order_send(request)
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