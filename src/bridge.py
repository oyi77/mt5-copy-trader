"""Core copy-trade bridge — polls master, detects changes, broadcasts to hub."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import MetaTrader5 as mt5

from src.config import Config, FollowerConfig
from src.master import MasterMonitor
from src.follower import FollowerExecutor
from src.state import SharedState

logger = logging.getLogger(__name__)


def _event_to_dict(event) -> dict:
    """Serialize TradeEvent to dict for JSON transmission."""
    return {
        "action": event.action,
        "symbol": event.symbol,
        "volume": event.volume,
        "price": event.price,
        "sl": event.sl,
        "tp": event.tp,
        "master_ticket": event.master_ticket,
        "position_type": event.position_type,
        "comment": event.comment,
        "magic": event.magic,
        "prev_volume": event.prev_volume,
    }


class CopyTradeBridge:
    """Main loop: poll master → detect changes → update state + queue.

    Runs in a dedicated thread. Puts trade events on asyncio.Queue for the
    hub to broadcast to connected agents.
    """

    def __init__(
        self,
        config: Config,
        state: SharedState,
        event_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ):
        self._cfg = config
        self._state = state
        self._queue = event_queue
        self._loop = loop
        self._master = MasterMonitor(config.master)
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking run — call from a thread."""
        logger.info("%s", "=" * 60)
        logger.info("BRIDGE - starting")
        logger.info("  Master port: %d", self._cfg.master.port)
        logger.info("  Poll interval: %d ms", self._cfg.poll_interval_ms)
        logger.info("%s", "=" * 60)

        self._state.stats.start_time = time.time()

        # Initial snapshot
        self._take_snapshot()
        self._state.stats.master_connected = True

        self._running = True
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Bridge cycle error")
                self._state.update_stats(errors=self._state.stats.errors + 1)
                time.sleep(2.0)

        self._log_shutdown()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self._state.update_stats(cycles=self._state.stats.cycles + 1)

        # 1. Poll master
        if not self._master.connect():
            self._state.stats.master_connected = False
            time.sleep(1.0)
            return

        try:
            positions = self._master.poll()
            account = self._get_account_info()
        finally:
            self._master.disconnect()

        self._state.stats.master_connected = True

        # 2. Update master state for dashboard
        self._update_master_state(positions, account)

        # 3. Detect changes
        events = self._master.detect_changes(positions)

        if not events:
            time.sleep(self._cfg.poll_interval_ms / 1000.0)
            return

        # 4. Record + broadcast events
        self._state.update_stats(
            events_detected=self._state.stats.events_detected + len(events),
            last_event_time=time.time(),
        )
        self._state.known_tickets = set(self._master.known_tickets)

        for event in events:
            logger.info(
                "EVENT: %s %s %.2f %s (ticket=%d)",
                event.action.upper(), event.symbol, event.volume,
                "BUY" if event.position_type == 0 else "SELL",
                event.master_ticket,
            )

        # Put events on the queue for broadcast to agents
        dict_events = [_event_to_dict(e) for e in events]
        self._loop.call_soon_threadsafe(self._queue.put_nowait, dict_events)

        time.sleep(0.1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _take_snapshot(self) -> None:
        logger.info("Taking master position snapshot...")
        if not self._master.connect():
            logger.error("Cannot take snapshot - master unreachable")
            return
        try:
            positions = self._master.poll()
            self._master.snapshot(positions)
            account = self._get_account_info()
            self._update_master_state(positions, account)
            logger.info("Master snapshot: %d positions", len(positions))
        finally:
            self._master.disconnect()

    def _update_master_state(self, positions, account) -> None:
        pos_list = []
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": p.type,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "swap": 0.0,
                "comment": p.comment,
                "magic": p.magic,
            })
        acc_info = {}
        if account:
            acc_info = {
                "balance": account.balance,
                "equity": account.equity,
                "margin": account.margin,
                "margin_free": account.margin_free,
                "leverage": account.leverage,
                "currency": account.currency,
                "login": account.login,
                "server": account.server,
                "name": account.name,
            }
        self._state.update_master(pos_list, acc_info)

    def _get_account_info(self):
        try:
            return mt5.account_info()
        except Exception:
            return None

    def _log_shutdown(self) -> None:
        stats = self._state.get_stats()
        logger.info("%s", "=" * 60)
        logger.info("BRIDGE STOPPED")
        logger.info("  Cycles : %d", stats["cycles"])
        logger.info("  Events : %d", stats["events_detected"])
        logger.info("  Errors : %d", stats["errors"])
        logger.info("  Uptime : %.0f s", stats["uptime"])
        logger.info("%s", "=" * 60)
