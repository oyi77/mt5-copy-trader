"""File-based trade relay (pending.txt / result.txt) for a follower.

This is a mixin (no ``__init__``) — the composing executor provides ``_cfg``,
``_name``, ``_file_data_path``, ``_dry_run``, and the ``_map_symbol`` /
``_apply_lot_scaling`` helpers defined by SymbolMappingMixin. It implements the
TradeReceiver.mq5 command protocol used by Exness-style custom builds that have
no MT5 IPC access.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from src.models import TradeEvent

logger = logging.getLogger(__name__)


class FileRelayMixin:
    """Execute trade events by writing commands to pending.txt and polling
    result.txt, which TradeReceiver.mq5 on the follower terminal consumes."""

    def _pending_path(self) -> str:
        return os.path.join(self._file_data_path, "pending.txt")

    def _result_path(self) -> str:
        return os.path.join(self._file_data_path, "result.txt")

    def _file_build_command(self, action: str, event: TradeEvent) -> str:
        """Build the pipe-delimited command line for TradeReceiver.mq5.

        Market commands use ACTION|SYMBOL|VOLUME|SL|TP|TICKET; pending-order
        commands carry extra fields (order type, price, expiration) and
        DELETE_ORDER needs only the master ticket.
        """
        symbol = self._map_symbol(event.symbol)
        volume = self._apply_lot_scaling(event.volume, symbol)
        ticket = event.master_ticket

        if action in ("PLACE_ORDER", "MODIFY_ORDER"):
            # ACTION|SYMBOL|OTYPE|VOLUME|PRICE|SL|TP|EXPIRATION|TICKET
            otype = event.order_type if event.order_type is not None else event.position_type
            price = f"{event.price:.5f}" if event.price else "0"
            sl_str = f"{event.sl:.5f}" if event.sl else ""
            tp_str = f"{event.tp:.5f}" if event.tp else ""
            exp = str(int(event.expiration)) if event.expiration else "0"
            return f"{action}|{symbol}|{otype}|{volume:.2f}|{price}|{sl_str}|{tp_str}|{exp}|{ticket}"
        if action == "DELETE_ORDER":
            return f"{action}|{ticket}"
        sl_str = f"{event.sl:.5f}" if event.sl else ""
        tp_str = f"{event.tp:.5f}" if event.tp else ""
        return f"{action}|{symbol}|{volume:.2f}|{sl_str}|{tp_str}|{ticket}"

    def _file_send_command(self, action: str, event: TradeEvent) -> Optional[str]:
        """Write a trade command to pending.txt, poll for result.txt.

        Returns the result string (e.g. "DONE|123456") or None on timeout.
        """
        cmd = self._file_build_command(action, event)

        pp = self._pending_path()
        rp = self._result_path()
        tmp = pp + ".tmp"

        # Clean any stale pending and temp files
        for f in (pp, tmp):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass

        # Clean any stale result file
        try:
            if os.path.exists(rp):
                os.remove(rp)
        except OSError:
            pass

        # Write pending command atomically: temp file then rename
        try:
            with open(tmp, "x", encoding="ascii") as f:
                f.write(cmd)
            os.replace(tmp, pp)
        except OSError as e:
            logger.error("%s: failed to write pending file: %s", self._name, e)
            return None

        logger.info(
            "%s: wrote pending command: %s", self._name, cmd,
        )

        # Poll for result (up to 30 seconds)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if os.path.exists(rp):
                try:
                    with open(rp, "r", encoding="ascii") as f:
                        result = f.read().strip()
                    os.remove(rp)
                    return result
                except OSError as e:
                    logger.warning("%s: error reading result: %s", self._name, e)
                    time.sleep(0.5)
                    continue
            time.sleep(0.3)

        logger.warning("%s: timeout waiting for result after 30s", self._name)
        return None

    def _file_execute_event(self, event: TradeEvent) -> bool:
        """Execute a trade event via file relay."""
        if self._dry_run:
            logger.info(
                "%s: DRY_RUN file would send action=%s symbol=%s volume=%.2f",
                self._name, event.action, event.symbol, event.volume,
            )
            return True
        if event.action == "open":
            cmd = "OPEN_BUY" if event.position_type == 0 else "OPEN_SELL"
        elif event.action == "close":
            cmd = "CLOSE"
        elif event.action == "modify":
            # TradeReceiver.mq5 supports MODIFY (TRADE_ACTION_SLTP on the
            # position found by its copied_<ticket> comment). SL/TP are sent
            # in the same command slots as OPEN_BUY.
            cmd = "MODIFY"
        elif event.action == "place":
            cmd = "PLACE_ORDER"
        elif event.action == "modify_order":
            cmd = "MODIFY_ORDER"
        elif event.action == "delete":
            cmd = "DELETE_ORDER"
        elif event.action == "close_all":
            cmd = "CLOSE_ALL"
        elif event.action == "ping":
            cmd = "PING"
        else:
            logger.error("%s: unknown action %s", self._name, event.action)
            return False

        result = self._file_send_command(cmd, event)
        if result is None:
            logger.error("%s: file command timed out for %s", self._name, event.action)
            return False

        if result.startswith("DONE"):
            parts = result.split("|")
            ticket = parts[1] if len(parts) > 1 else "0"
            logger.info(
                "%s: %s (file) -> DONE ticket=%s",
                self._name, event.action.upper(), ticket,
            )
            return True
        if result.startswith("FAILED|NF"):
            # Not found — the follower has nothing matching this ticket (e.g.
            # the bridge was down when the open/place was broadcast, or a
            # previous attempt already succeeded). The desired end state
            # (nothing left to close/modify/delete) already holds, so this is
            # benign — log at info, not error, and do NOT enqueue a retry.
            logger.info(
                "%s: %s (file) -> %s — already consistent, nothing to do",
                self._name, event.action.upper(), result,
            )
            return True
        logger.error(
            "%s: %s (file) -> %s", self._name, event.action.upper(), result,
        )
        return False

    def _file_get_status(self) -> dict:
        """Return placeholder status for file-based mode (no IPC)."""
        # Send PING to verify EA is alive. Use a configured symbol (first
        # symbol_mapping value) instead of a hard-coded one; PING ignores it,
        # but the relay file stays valid for the mapped account.
        ping_symbol = next(iter(self._cfg.symbol_mapping.values()), "XAUUSDc")
        ping_event = TradeEvent(
            action="ping", symbol=ping_symbol, volume=0.01,
            price=0.0, sl=None, tp=None,
            master_ticket=0, position_type=0,
            comment="", magic=0,
        )
        alive = self._file_send_command("PING", ping_event)

        return {
            "name": self._name,
            "active": True,
            "connected": True,
            "trade_allowed": True,
            "file_based": True,
            "account_login": self._cfg.login,
            "server": self._cfg.server,
            "balance": 0,
            "equity": 0,
            "positions": [],
            "position_count": 0,
            "ea_alive": alive is not None and "DONE|PONG" in alive,
        }