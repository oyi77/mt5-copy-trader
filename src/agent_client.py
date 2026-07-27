"""Agent WebSocket client — runs on each follower machine."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
from typing import Optional

import aiohttp

from src.follower import FollowerExecutor
from src.config import FollowerConfig

logger = logging.getLogger(__name__)


class AgentClient:
    """Connects to the hub, receives trade events, executes on local MT5."""

    def __init__(
        self,
        hub_url: str,
        follower_cfg: FollowerConfig,
        agent_name: str,
    ):
        self._hub_url = hub_url.rstrip("/")
        self._follower_cfg = follower_cfg
        self._agent_name = agent_name
        self._executor = FollowerExecutor(follower_cfg)
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._last_status_send = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run forever, reconnecting on disconnect."""
        self._running = True
        retry_delay = 1.0

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except aiohttp.ClientConnectorDNSError:
                logger.error(
                    "Cannot resolve hub hostname '%s'. "
                    "Check that:\n"
                    "  1. The hub IP/hostname in agent_config.yaml is correct\n"
                    "  2. If using Tailscale: Tailscale is connected on THIS machine\n"
                    "  3. The hub IP is reachable (try: ping <hub_ip>)",
                    self._hub_url
                )
            except aiohttp.ClientConnectorError:
                logger.error(
                    "Cannot connect to hub at %s. "
                    "Check that:\n"
                    "  1. The hub server is running on the master machine\n"
                    "  2. Port 5000 is not blocked by a firewall\n"
                    "  3. If using Tailscale: both machines are on the same tailnet",
                    self._hub_url
                )
            except asyncio.TimeoutError:
                logger.error("Connection to hub timed out — check network and firewall")
            except Exception:
                logger.exception("Connection error")

            if not self._running:
                break

            logger.info("Reconnecting in %.1f seconds...", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def _connect_and_listen(self) -> None:
        ws_url = f"{self._hub_url.replace('http://', 'ws://').replace('https://', 'wss://')}/ws/agent?name={urllib.parse.quote(self._agent_name, safe='')}"
        logger.info("Connecting to hub: %s", ws_url.replace(self._agent_name, '[hidden]'))

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        async with self._session.ws_connect(
            ws_url,
            heartbeat=15.0,
            receive_timeout=30.0,
            max_msg_size=256 * 1024,
        ) as ws:
            self._ws = ws
            logger.info("Connected to hub as '%s'", self._agent_name)

            # Send initial status immediately
            await self._send_status()

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", ws.exception())
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    break

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from hub")
            return

        msg_type = data.get("type", "")

        if msg_type == "trade":
            event = data.get("event", {})
            await self._execute_event(event)

        elif msg_type == "ping":
            ts = data.get("timestamp", 0)
            try:
                await self._ws.send_str(json.dumps({
                    "type": "pong",
                    "timestamp": ts,
                }))
            except Exception:
                pass

        # Send status periodically (every 5s) or when positions likely changed
        now = time.time()
        if now - self._last_status_send > 5.0:
            await self._send_status()

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    async def _execute_event(self, event_data: dict) -> None:
        """Execute a trade event and send result back to hub."""
        from src.models import TradeEvent

        event = TradeEvent(
            action=event_data.get("action", ""),
            symbol=event_data.get("symbol", ""),
            volume=event_data.get("volume", 0.0),
            price=event_data.get("price", 0.0),
            sl=event_data.get("sl"),
            tp=event_data.get("tp"),
            master_ticket=event_data.get("master_ticket", 0),
            position_type=event_data.get("position_type", 0),
            comment=event_data.get("comment", ""),
            magic=event_data.get("magic", 0),
            prev_volume=event_data.get("prev_volume"),
        )

        logger.info("EXECUTING: %s %s %.2f (ticket=%d)", event.action.upper(), event.symbol, event.volume, event.master_ticket)

        success = False
        follower_ticket = 0

        # Connect to local MT5 and execute
        if self._executor.connect():
            try:
                success = self._executor.execute(event)
                # If open succeeded, find the ticket (last opened)
                if success and event.action == "open":
                    positions = mt5_positions_get()
                    if positions:
                        follower_ticket = positions[-1].ticket
            except Exception:
                logger.exception("Execution error")
            finally:
                self._executor.disconnect()
        else:
            logger.error("Cannot connect to local MT5")

        # Send result back
        result = {
            "type": "execution_result",
            "master_ticket": event.master_ticket,
            "action": event.action,
            "success": success,
            "follower_ticket": follower_ticket,
            "error": None if success else "execution_failed",
        }
        try:
            await self._ws.send_str(json.dumps(result))
        except Exception:
            pass

        # Send updated status
        await self._send_status()

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    async def _send_status(self) -> None:
        """Send current account/position status to hub."""
        self._last_status_send = time.time()

        if not self._executor.connect():
            status = {
                "type": "status",
                "connected": False,
                "balance": 0, "equity": 0, "margin": 0, "margin_free": 0,
                "margin_level": 0, "leverage": 0,
                "currency": "", "server": "", "account_name": "", "account_login": 0,
                "unrealized_pnl": 0, "daily_pnl": 0, "total_pnl": 0,
                "positions": [], "position_count": 0,
            }
        else:
            try:
                account = get_mt5_account_info()
                positions = mt5_positions_get()

                pos_list = []
                unrealized_pnl = 0.0
                if positions:
                    for p in positions:
                        pnl = (p.profit or 0) + (p.swap or 0)
                        unrealized_pnl += pnl
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
                            "swap": p.swap,
                            "comment": p.comment,
                            "magic": p.magic,
                        })

                equity = account.equity if account else 0
                margin = account.margin if account else 0
                margin_level = (equity / margin * 100) if margin > 0 else 0.0

                status = {
                    "type": "status",
                    "connected": True,
                    "balance": account.balance if account else 0,
                    "equity": equity,
                    "margin": margin,
                    "margin_free": account.margin_free if account else 0,
                    "margin_level": round(margin_level, 2),
                    "leverage": account.leverage if account else 0,
                    "currency": account.currency if account else "USD",
                    "server": account.server if account else "",
                    "account_name": account.name if account else "",
                    "account_login": account.login if account else 0,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "daily_pnl": 0.0,
                    "total_pnl": 0.0,
                    "positions": pos_list,
                    "position_count": len(pos_list),
                }
            except Exception:
                logger.exception("Failed to get MT5 status")
                status = {"type": "status", "connected": False, "positions": [], "position_count": 0}
            finally:
                self._executor.disconnect()

        try:
            await self._ws.send_str(json.dumps(status))
        except Exception:
            pass


# ── Standalone MT5 helpers (used without FollowerExecutor context) ──

def get_mt5_account_info():
    import MetaTrader5 as mt5
    return mt5.account_info()


def mt5_positions_get():
    import MetaTrader5 as mt5
    return mt5.positions_get()
