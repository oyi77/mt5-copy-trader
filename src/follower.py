"""Follower MT5 terminal trade execution."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

import MetaTrader5 as mt5

from src.config import FollowerConfig
from src.models import TradeEvent

logger = logging.getLogger(__name__)


class FollowerExecutor:
    """Connects to a follower MT5 terminal and executes trade events."""

    def __init__(self, config: FollowerConfig, master_port: int = 0):
        self._cfg = config
        self._name = config.name
        self._master_port = master_port
        self._process: Optional[subprocess.Popen] = None
        self._exe_path: str = config.path

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def launch_terminal(self, timeout: float = 15.0) -> bool:
        """Ensure the MT5 terminal is running and accessible.

        On same-machine followers, this reuses the master's terminal process
        (avoids needing a separate MT5 installation). The terminal is already
        running since the bridge keeps the master connected.

        The follower connects via mt5.initialize() with login/password/server,
        which logs into the follower's account on the same terminal.
        """
        import os

        logger.info("%s: same-machine — reusing master terminal via account switching", self._name)
        self._exe_path = self._cfg.path
        logger.info("%s: terminal ready (shares master's MT5 installation)", self._name)
        return True

    def connect(self, master_port: int = 0) -> bool:
        """Initialize MT5 API connection to this follower's terminal.

        For same-machine followers, the master's terminal is reused and
        account switching happens via mt5.initialize(login, password, server).
        The master_port parameter should be set when the follower shares
        the master's terminal.
        """
        port = master_port or self._master_port or self._cfg.port
        result = mt5.initialize(
            path=self._exe_path,
            port=port,
            login=self._cfg.login,
            password=self._cfg.password,
            server=self._cfg.server,
            timeout=60000,  # generous timeout for first-time login
        )
        if not result:
            logger.error("%s: connect failed: %s", self._name, mt5.last_error())
        else:
            logger.info("%s: connected successfully", self._name)
        return result

    def disconnect(self) -> None:
        """Shutdown MT5 API connection."""
        mt5.shutdown()

    def is_connected(self) -> bool:
        """Check if the MT5 terminal is reachable on the configured port."""
        return mt5.initialize(path=self._exe_path, port=self._cfg.port)

    def _check_running(self) -> bool:
        """Quick check: can we reach the terminal?"""
        try:
            return mt5.initialize(path=self._exe_path, port=self._cfg.port, timeout=2000)
        except Exception:
            return False
        finally:
            mt5.shutdown()

    # ------------------------------------------------------------------
    # Execute (with auto-connect/disconnect per call)
    # ------------------------------------------------------------------

    def execute(self, event: TradeEvent) -> bool:
        """Execute a single trade event on this follower.

        Auto-connects before and disconnects after.
        Returns True if the operation was submitted successfully.
        """
        symbol = self._map_symbol(event.symbol)
        volume = self._apply_lot_scaling(event.volume)

        if not self.connect():
            return False
        try:
            if event.action == "open":
                return self._open(symbol, volume, event)
            elif event.action == "close":
                return self._close(symbol, event)
            elif event.action == "modify":
                return self._modify(symbol, event, volume)
            elif event.action == "place":
                return self._place_order(symbol, volume, event)
            elif event.action == "modify_order":
                return self._modify_order(symbol, event)
            elif event.action == "delete":
                return self._delete_order(symbol, event)
            else:
                logger.error("%s: Unknown action %s", self._name, event.action)
                return False
        finally:
            self.disconnect()

    def get_status(self) -> dict:
        """Return status dict for dashboard display."""
        info = {"name": self._name, "active": True}
        try:
            ok = mt5.initialize(path=self._exe_path, port=self._cfg.port, timeout=2000)
            info["connected"] = ok
            if ok:
                acc = mt5.account_info()
                if acc:
                    info["balance"] = acc.balance
                    info["equity"] = acc.equity
                    info["login"] = acc.login
                    info["server"] = acc.server
                mt5.shutdown()
        except Exception:
            info["connected"] = False
        finally:
            try:
                mt5.shutdown()
            except Exception:
                pass
        return info

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

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
        close_vol = event.volume_change()
        if close_vol:
            self._partial_close(symbol, fpos, close_vol, event)
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
                "sl": self._map_price(event.sl) if event.sl else 0.0,
                "tp": self._map_price(event.tp) if event.tp else 0.0,
                "magic": self._cfg.magic,
                "comment": fpos.comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
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

    # ------------------------------------------------------------------
    # Pending order actions
    # ------------------------------------------------------------------

    ORDER_TYPE_MAP = {
        2: mt5.ORDER_TYPE_BUY_LIMIT,
        3: mt5.ORDER_TYPE_SELL_LIMIT,
        4: mt5.ORDER_TYPE_BUY_STOP,
        5: mt5.ORDER_TYPE_SELL_STOP,
    }

    def _place_order(self, symbol: str, volume: float, event: TradeEvent) -> bool:
        order_type = self.ORDER_TYPE_MAP.get(event.order_type or event.position_type)
        if order_type is None:
            logger.error("%s: PLACE — unknown order type %s", self._name, event.order_type)
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
            "sl": self._map_price(event.sl) if event.sl else 0.0,
            "tp": self._map_price(event.tp) if event.tp else 0.0,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": str(event.master_ticket),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if event.expiration:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = event.expiration

        result = mt5.order_send(request)
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
            "sl": self._map_price(event.sl) if event.sl else 0.0,
            "tp": self._map_price(event.tp) if event.tp else 0.0,
            "magic": self._cfg.magic,
            "comment": f_order.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if event.expiration:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = event.expiration

        result = mt5.order_send(request)
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

        result = mt5.order_send(request)
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

    def _find_order_by_comment(self, comment: str):
        """Find a pending order with the given comment string."""
        orders = mt5.orders_get()
        if orders is None:
            return None
        for order in orders:
            if order.comment == comment:
                return order
        return None

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
        if positions is None:
            return None
        for pos in positions:
            if pos.comment == comment:
                return pos
        return None

    def _map_symbol(self, symbol: str) -> str:
        mapping = self._cfg.symbol_mapping
        upper = symbol.upper()
        return mapping.get(upper, upper)

    def _apply_lot_scaling(self, volume: float) -> float:
        vol = volume * self._cfg.lot_multiplier
        if self._cfg.max_lot > 0:
            vol = min(vol, self._cfg.max_lot)
        if self._cfg.min_lot > 0:
            vol = max(vol, self._cfg.min_lot)
        return round(vol, 2)

    def _map_price(self, price: float) -> float:
        """Round to 5 decimals (standard forex precision)."""
        return round(price, 5)

    def _get_price(self, symbol: str, order_type: int) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("%s: no tick for %s", self._name, symbol)
            return None
        return tick.ask if order_type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP) else tick.bid

    def _mask_request(self, req: dict) -> dict:
        """Remove sensitive fields for logging."""
        masked = dict(req)
        masked.pop("password", None)
        return masked
