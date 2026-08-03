"""Agent WebSocket client — runs on each follower machine as a daemon.

Connects to the hub, registers itself with identity info, receives trade
events and config updates, executes on local MT5. Auto-reconnects on
disconnect with exponential backoff.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import platform
import random
import time
import urllib.parse
from typing import Optional

import aiohttp

from src.follower import FollowerExecutor
from src.config import FollowerConfig

logger = logging.getLogger(__name__)

AGENT_VERSION = "1.0.0"


def _parse_bool(value, default: bool = False) -> bool:
    """Parse a boolean, accepting true/false/yes/no/1/0.

    JSON payloads often carry booleans as strings ('false', '1'), which
    Python's bool() would wrongly treat as True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return default


def _parse_int(value, name: str, minv: Optional[int] = None, maxv: Optional[int] = None) -> int:
    """Parse an int, raising ValueError with the field name on bad input."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: invalid integer value {value!r}")
    if minv is not None and v < minv:
        raise ValueError(f"{name}: {v} below minimum {minv}")
    if maxv is not None and v > maxv:
        raise ValueError(f"{name}: {v} above maximum {maxv}")
    return v


def _parse_float(value, name: str, minv: Optional[float] = None, maxv: Optional[float] = None) -> float:
    """Parse a float, raising ValueError with the field name on bad input."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: invalid numeric value {value!r}")
    if minv is not None and v < minv:
        raise ValueError(f"{name}: {v} below minimum {minv}")
    if maxv is not None and v > maxv:
        raise ValueError(f"{name}: {v} above maximum {maxv}")
    return v


class AgentClient:
    """Connects to the hub, receives trade events, executes on local MT5."""

    def __init__(
        self,
        hub_url: str,
        follower_cfg: Optional[FollowerConfig] = None,
        agent_name: str = "",
        agent_id: str = "",
        event_store_path: Optional[str] = None,
        config_save_path: str = "agent_config.yaml",
    ):
        self._hub_url = hub_url.rstrip("/")
        self._follower_cfg = follower_cfg
        self._agent_name = agent_name
        self._agent_id = agent_id or agent_name
        self._configured = follower_cfg is not None
        self._config_save_path = config_save_path
        if self._configured:
            self._executor = FollowerExecutor(follower_cfg)
        else:
            self._executor = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._last_status_send = 0.0
        self._hostname = platform.node()
        self._event_store_path = event_store_path
        # seq-file path for tracking last-seen event ID (for replay on reconnect)
        self._seq_path = event_store_path or (
            (follower_cfg.queue_path + ".seq") if self._configured else (config_save_path + ".seq")
        )
        self._platform = f"{platform.system()} {platform.release()}"
        # All MetaTrader5 access is NOT thread-safe — serialize every call
        # (execute/connect/status) through this single-worker pool.
        self._mt5_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mt5"
        )
        self._run_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Seq-id persistence (for event replay on reconnect)
    # ------------------------------------------------------------------

    def _load_last_seq(self) -> int:
        try:
            with open(self._seq_path, "r") as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_last_seq(self, seq_id: int) -> None:
        try:
            with open(self._seq_path, "w") as f:
                f.write(str(seq_id))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run forever, reconnecting on disconnect."""
        self._running = True
        self._run_task = asyncio.current_task()
        retry_delay = 1.0

        while self._running:
            try:
                await self._connect_and_listen()
                # Clean close (hub/network closed a working connection): decay
                # the backoff gradually instead of resetting to 1.0, so a
                # disconnect-flap still backs off.
                retry_delay = max(retry_delay / 2, 1.0)
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

            logger.info("%s: Reconnecting in %.1f seconds...", self._agent_id, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    def stop(self) -> None:
        """Stop the agent: cancel the in-flight connection and close WS/session.

        Sync entry point (signal handlers, other threads): schedules the async
        cleanup on the running loop, so shutdown does not hang on the current
        connect/receive cycle.
        """
        self._running = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.create_task, self._shutdown())

    async def _shutdown(self) -> None:
        """Cancel the run loop task, then close WS and session (best effort)."""
        run_task = self._run_task
        if run_task is not None and not run_task.done() and run_task is not asyncio.current_task():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        session = self._session
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                pass
        # Stop accepting new MT5 work; in-flight op finishes in the background.
        self._mt5_pool.shutdown(wait=False)

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

            # Send registration on connect
            await self._send_registration()

            # Replay any events queued during disconnect (configured mode only)
            if self._configured:
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self._mt5_pool, self._executor._dequeue_and_replay
                    )
                except Exception:
                    logger.exception("Failed to replay queued events")

            # Send initial status immediately
            await self._send_status()

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error from hub: %s", ws.exception())
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.info("Hub closed WebSocket (code=%s): %s", msg.data, ws.close_code or '?')
                    break

        # If we exit the ws context manager gracefully (no exception),
        # the connection was closed by the hub or network
        if self._running:
            logger.info("%s: Disconnected from hub — will reconnect", self._agent_id)

    async def _send_registration(self) -> None:
        """Send agent identity to hub so it knows who we are."""
        reg = {
            "type": "register",
            "agent_id": self._agent_id,
            "name": self._agent_name,
            "version": AGENT_VERSION,
            "hostname": self._hostname,
            "platform": self._platform,
            "last_seq_id": self._load_last_seq(),
            "status": "trading" if self._configured else "unconfigured",
        }
        try:
            await self._ws.send_str(json.dumps(reg))
            logger.info("Registered with hub: %s v%s on %s", self._agent_name, AGENT_VERSION, self._hostname)
        except Exception:
            logger.exception("Failed to send registration")

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from hub")
            return

        msg_type = data.get("type", "")
        logger.debug("RX message type=%s keys=%s", msg_type, list(data.keys()))

        if msg_type == "trade":
            if not self._configured:
                logger.warning("Ignoring trade event — agent not configured yet")
                return
            event = data.get("event", {})
            if self._is_own_event(event):
                return
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

        elif msg_type == "config_update":
            if not self._configured:
                logger.warning("Ignoring config_update — agent not fully deployed yet")
            else:
                await self._handle_config_update(data.get("config", {}))

        elif msg_type == "config_deploy":
            await self._handle_config_deploy(data.get("config", {}))

        # Send status periodically (every 5s) or when positions likely changed
        now = time.time()
        if now - self._last_status_send > 5.0:
            await self._send_status()

    async def _handle_config_update(self, config: dict) -> None:
        """Apply config overrides pushed from the hub/dashboard.

        Invalid values are rejected per-field (logged and reported in the ack)
        instead of raising and killing the WS connection.
        """
        if not config:
            return

        errors: list[str] = []
        changed: list[str] = []
        cfg = self._follower_cfg

        def _field_float(key: str, minv: Optional[float] = None) -> Optional[float]:
            if key not in config:
                return None
            try:
                return _parse_float(config[key], key, minv=minv)
            except ValueError as e:
                errors.append(str(e))
                return None

        def _field_int(key: str, minv: Optional[int] = None) -> Optional[int]:
            if key not in config:
                return None
            try:
                return _parse_int(config[key], key, minv=minv)
            except ValueError as e:
                errors.append(str(e))
                return None

        v = _field_float("lot_multiplier", minv=0.0)
        if v is not None:
            old = cfg.lot_multiplier
            cfg.lot_multiplier = v
            changed.append(f"lot_multiplier: {old} → {cfg.lot_multiplier}")

        v = _field_float("max_lot", minv=0.0)
        if v is not None:
            old = cfg.max_lot
            cfg.max_lot = v
            changed.append(f"max_lot: {old} → {cfg.max_lot}")

        v = _field_float("min_lot", minv=0.0)
        if v is not None:
            old = cfg.min_lot
            cfg.min_lot = v
            changed.append(f"min_lot: {old} → {cfg.min_lot}")

        v = _field_int("max_positions", minv=0)
        if v is not None:
            old = cfg.max_positions
            cfg.max_positions = v
            changed.append(f"max_positions: {old} → {cfg.max_positions}")

        v = _field_int("deviation", minv=0)
        if v is not None:
            old = cfg.deviation
            cfg.deviation = v
            changed.append(f"deviation: {old} → {cfg.deviation}")

        v = _field_int("magic", minv=0)
        if v is not None:
            old = cfg.magic
            cfg.magic = v
            changed.append(f"magic: {old} → {cfg.magic}")

        if "queue_path" in config:
            old = cfg.queue_path
            new_qp = config["queue_path"]
            if not isinstance(new_qp, str) or not new_qp.strip():
                errors.append(f"queue_path: invalid value {new_qp!r}")
            else:
                cfg.queue_path = new_qp
                changed.append(f"queue_path: {old} → {cfg.queue_path}")
                # Seq file follows the queue location
                self._seq_path = self._event_store_path or (cfg.queue_path + ".seq")

        if "symbol_mapping" in config:
            if isinstance(config["symbol_mapping"], dict):
                cfg.symbol_mapping = {k.upper(): v for k, v in config["symbol_mapping"].items()}
                changed.append(f"symbol_mapping: {len(cfg.symbol_mapping)} entries")
            else:
                errors.append(
                    f"symbol_mapping: expected an object, got {type(config['symbol_mapping']).__name__}"
                )

        if errors:
            logger.warning("Config update rejected field(s): %s", "; ".join(errors))
        if changed:
            logger.info("Config updated from hub: %s", "; ".join(changed))
        else:
            logger.info("Config update received (no applicable fields): %s", config)

        # Reconnect executor with new config
        self._executor = FollowerExecutor(self._follower_cfg)

        # Report back
        try:
            await self._ws.send_str(json.dumps({
                "type": "config_ack",
                "applied": bool(changed),
                "ok": not errors,
                "error": "; ".join(errors) if errors else None,
                "config": config,
            }))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Config deploy (unconfigured → configured transition)
    # ------------------------------------------------------------------

    async def _handle_config_deploy(self, config: dict) -> None:
        """Receive full agent config from dashboard, deploy MT5, start trading."""
        import yaml

        if self._configured:
            logger.warning("Already configured, ignoring deploy")
            return

        config_path = self._config_save_path
        logger.info("Received deploy config, saving to %s", config_path)

        # Resolve queue path to an absolute location next to the saved config
        # (survives scheduled-task/SSH cwd differences)
        queue_path = config.get("queue_path", "trade_queue.json")
        if not os.path.isabs(queue_path):
            queue_path = os.path.join(os.path.dirname(config_path), queue_path)

        # Validate numeric values before touching anything on disk
        try:
            port = _parse_int(config.get("port", 0), "port", minv=0, maxv=65535)
            login = _parse_int(config.get("login", 0), "login", minv=0)
            lot_multiplier = _parse_float(config.get("lot_multiplier", 1.0), "lot_multiplier", minv=0.0)
            max_lot = _parse_float(config.get("max_lot", 10.0), "max_lot", minv=0.0)
            min_lot = _parse_float(config.get("min_lot", 0.01), "min_lot", minv=0.0)
            max_positions = _parse_int(config.get("max_positions", 10), "max_positions", minv=0)
            deviation = _parse_int(config.get("deviation", 50), "deviation", minv=0)
            magic = _parse_int(config.get("magic", 951001), "magic", minv=0)
            max_daily_loss = _parse_float(config.get("max_daily_loss", 0.0), "max_daily_loss", minv=0.0)
            max_drawdown_pct = _parse_float(config.get("max_drawdown_pct", 0.0), "max_drawdown_pct", minv=0.0)
            max_daily_trades = _parse_int(config.get("max_daily_trades", 0), "max_daily_trades", minv=0)
        except ValueError as e:
            logger.warning("Rejecting deploy config: %s", e)
            await self._send_status_update("error", f"Invalid deploy config: {e}")
            return

        raw_mapping = config.get("symbol_mapping", {})
        if not isinstance(raw_mapping, dict):
            logger.warning("Deploy config: symbol_mapping ignored (not an object)")
            raw_mapping = {}
        symbol_mapping = {k.upper(): v for k, v in raw_mapping.items()}

        # Save config in AgentConfig schema (mt5_login/mt5_path/...) so that
        # load_agent_config() succeeds on restart. The dashboard payload uses
        # FollowerConfig keys (login/path/port/...) — convert here.
        # hub_url is taken from the connection the agent is actually using
        # (e.g. the reverse-tunnel URL), and log_file is forced into the
        # user-writable data dir (filtered scheduled-task tokens can't write
        # relative paths like C:\logs\agent.log).
        data_dir = os.path.dirname(config_path)
        agent_config = {
            "name": config.get("name", self._agent_name),
            "hub_url": self._hub_url,
            "mt5_path": config.get("path", "C:/Program Files/MetaTrader 5/terminal64.exe"),
            "mt5_port": port,
            "mt5_login": login,
            "mt5_password": config.get("password", ""),
            "mt5_server": config.get("server", ""),
            "lot_multiplier": lot_multiplier,
            "max_lot": max_lot,
            "min_lot": min_lot,
            "max_positions": max_positions,
            "deviation": deviation,
            "magic": magic,
            "skip_own_magic": _parse_bool(config.get("skip_own_magic", True), True),
            "symbol_mapping": symbol_mapping,
            "skip_auto_trading": _parse_bool(config.get("skip_auto_trading", True), True),
            "dry_run": _parse_bool(config.get("dry_run", False), False),
            "terminal_data_path": config.get("terminal_data_path", ""),
            "max_daily_loss": max_daily_loss,
            "max_drawdown_pct": max_drawdown_pct,
            "max_daily_trades": max_daily_trades,
            "queue_path": queue_path,
            "log_file": os.path.join(data_dir, "logs", "agent.log"),
            "log_level": "INFO",
        }

        if config.get("password"):
            logger.warning(
                "agent_config.yaml stores the MT5 password in plaintext (%s)",
                config_path,
            )

        # Save config to YAML (AgentConfig schema with resolved absolute paths)
        with open(config_path, "w") as f:
            yaml.dump(agent_config, f, default_flow_style=False, sort_keys=False)

        # Install MT5 if needed
        if _parse_bool(config.get("install_mt5", True), True):
            mt5_path = config.get("path", "")
            if not os.path.exists(mt5_path):
                logger.info("MT5 not found at %s, installing...", mt5_path)
                try:
                    from agent import install_standard_mt5
                    success = install_standard_mt5()
                    if not success:
                        logger.error("Failed to install MT5")
                        await self._send_status_update("error", "MT5 installation failed")
                        return
                    logger.info("MT5 installed successfully")
                except Exception as e:
                    logger.exception("MT5 installation error")
                    await self._send_status_update("error", f"MT5 install failed: {e}")
                    return

        # Create FollowerConfig from deploy config
        from src.config import FollowerConfig
        cfg = FollowerConfig(
            name=config.get("name", self._agent_name),
            path=config.get("path", "C:\\Program Files\\MetaTrader 5\\terminal64.exe"),
            port=port,
            login=login,
            password=config.get("password", ""),
            server=config.get("server", ""),
            lot_multiplier=lot_multiplier,
            max_lot=_parse_float(config.get("max_lot", 1.0), "max_lot", minv=0.0),
            min_lot=min_lot,
            max_positions=max_positions,
            deviation=deviation,
            magic=magic,
            skip_own_magic=_parse_bool(config.get("skip_own_magic", True), True),
            symbol_mapping=symbol_mapping,
            skip_auto_trading=_parse_bool(config.get("skip_auto_trading", True), True),
            terminal_data_path=config.get("terminal_data_path", ""),
            max_daily_loss=max_daily_loss,
            max_drawdown_pct=max_drawdown_pct,
            max_daily_trades=max_daily_trades,
            queue_path=queue_path,
        )

        self._follower_cfg = cfg
        self._executor = FollowerExecutor(cfg)
        self._configured = True
        # Replace seq-file location now that we know the real queue path
        self._seq_path = self._event_store_path or (queue_path + ".seq")
        self._config_save_path = config_path

        # Update agent name if provided
        if config.get("name"):
            self._agent_name = config["name"]

        # Validate MT5 connection (connect and immediately disconnect)
        await self._send_status_update("deploying", "Connecting to MT5...")
        if not self._executor.is_file_based():
            try:
                loop = asyncio.get_running_loop()
                conn_ok = await loop.run_in_executor(self._mt5_pool, self._executor.connect)
                if conn_ok:
                    logger.info("MT5 connection validated")
                    await loop.run_in_executor(self._mt5_pool, self._executor.disconnect)
                else:
                    logger.warning("Could not connect to MT5 with provided config")
                    await self._send_status_update("error", "MT5 connection failed")
                    # Continue anyway — will retry on status/event
            except Exception as e:
                logger.exception("MT5 connection error during deploy")
                await self._send_status_update("error", f"MT5 connect failed: {e}")

        # Send updated registration so hub knows we're now configured
        await self._send_registration()

        await self._send_status_update("trading", "Agent deployed and trading")
        logger.info("Agent deployed successfully")

    async def _send_status_update(self, status: str, message: str = "") -> None:
        """Send a lightweight status update to the hub."""
        try:
            await self._ws.send_str(json.dumps({
                "type": "status_update",
                "status": status,
                "message": message,
                "agent_id": self._agent_id,
            }))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _is_own_event(self, event_data: dict) -> bool:
        """True if the event is a trade this agent itself placed.

        The master broadcasts every change on its account. When a follower
        runs on the SAME account (e.g. testing a master+follower on one demo
        account), its own execution is re-broadcast with the follower's magic
        and would be copied again — an endless loop. Skipping events whose
        magic equals the follower's own magic breaks that loop. Guarded by
        ``skip_own_magic`` (default on); master and follower must therefore
        use different magic numbers.
        """
        cfg = self._follower_cfg
        if not (cfg and cfg.skip_own_magic):
            return False
        event_magic = int(event_data.get("magic", 0) or 0)
        if event_magic and event_magic == cfg.magic:
            seq_id = event_data.get("_seq_id", event_data.get("id", 0))
            logger.info(
                "Skipping own trade (magic %d == follower magic %d, seq=%s)",
                event_magic, cfg.magic, seq_id,
            )
            return True
        return False

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

        seq_id = event_data.get("_seq_id", event_data.get("id", 0))
        logger.info("EXECUTING: %s %s %.2f (ticket=%d, seq=%d)", event.action.upper(), event.symbol, event.volume, event.master_ticket, seq_id)

        # Execute on the serialized MT5 worker (MetaTrader5 is not thread-safe)
        loop = asyncio.get_running_loop()
        try:
            success, follower_ticket = await loop.run_in_executor(
                self._mt5_pool, self._execute_event_sync, event
            )
        except Exception:
            logger.exception("Execution error")
            success, follower_ticket = False, 0

        # Persist last-seen seq id ONLY on successful execution, so a failed
        # event is replayed by the hub after reconnect.
        if seq_id and success:
            self._save_last_seq(seq_id)

        # Send result back
        result = {
            "type": "execution_result",
            "master_ticket": event.master_ticket,
            "action": event.action,
            "success": success,
            "follower_ticket": follower_ticket,
            "seq_id": seq_id,
            "event_id": event_data.get("_seq_id", 0),
            "queue_path": self._follower_cfg.queue_path,
            "error": None if success else "execution_failed",
        }
        try:
            await self._ws.send_str(json.dumps(result))
        except Exception:
            pass

        # Send updated status
        await self._send_status()

    def _execute_event_sync(self, event: TradeEvent) -> tuple[bool, int]:
        """Blocking trade execution — runs in the serialized MT5 worker thread."""
        success = False
        follower_ticket = 0

        if self._executor.is_file_based():
            # File-based: just exec, no IPC calls
            try:
                success = self._executor.execute(event)
            except Exception:
                logger.exception("Execution error")
            return success, 0

        if self._executor.connect():
            try:
                success = self._executor.execute(event)
                # If open succeeded, attribute the ticket of the position this
                # order_send actually created, not the last list entry.
                if success and event.action == "open":
                    follower_ticket = self._lookup_open_ticket(event)
            except Exception:
                logger.exception("Execution error")
            finally:
                self._executor.disconnect()
        else:
            logger.error("Cannot connect to local MT5")
        return success, follower_ticket

    def _lookup_open_ticket(self, event: TradeEvent) -> int:
        """Return the ticket of the position just opened for this event, or 0.

        execute() disconnects internally after the order_send, so briefly
        reconnect to read the position list. Prefer the position matching this
        master ticket's comment+magic (the one order_send created); fall back
        to the last position returned by MT5.
        """
        try:
            if not self._executor.connect():
                return 0
            try:
                pos = self._executor._find_position_by_comment(str(event.master_ticket))
                if pos is not None:
                    return pos.ticket
                positions = mt5_positions_get()
                if positions:
                    return positions[-1].ticket
                return 0
            finally:
                self._executor.disconnect()
        except Exception:
            logger.exception("Failed to look up opened position ticket")
            return 0

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    async def _send_status(self) -> None:
        """Send current account/position status to hub."""
        if not self._configured:
            # Minimal heartbeat for unconfigured agents
            status = {
                "type": "status",
                "connected": False,
                "agent_id": self._agent_id,
                "version": AGENT_VERSION,
                "hostname": self._hostname,
                "platform": self._platform,
                "status": "unconfigured",
                "balance": 0,
                "equity": 0,
                "positions": [],
                "position_count": 0,
            }
        else:
            loop = asyncio.get_running_loop()
            executor = self._executor
            status = await loop.run_in_executor(
                self._mt5_pool, self._fetch_status_sync, executor
            )

        try:
            await self._ws.send_str(json.dumps(status))
            # Only stamp the send time after a successful send, so a failed
            # send retries on the next cycle instead of being throttled.
            self._last_status_send = time.time()
        except Exception as e:
            logger.warning("%s: send_status failed (connection may be down): %s", self._agent_id, e)

    def _fetch_status_sync(self, executor: FollowerExecutor) -> dict:
        """Synchronous MT5 status fetch — runs in thread executor."""
        # File-based: use executor's own status (no IPC)
        if executor.is_file_based():
            st = executor.get_status()
            st.update({
                "type": "status",
                "agent_id": self._agent_id,
                "version": AGENT_VERSION,
                "hostname": self._hostname,
                "platform": self._platform,
            })
            return st

        if not executor.connect():
            return {
                "type": "status",
                "connected": False,
                "agent_id": self._agent_id,
                "version": AGENT_VERSION,
                "hostname": self._hostname,
                "platform": self._platform,
                "balance": 0, "equity": 0, "margin": 0, "margin_free": 0,
                "margin_level": 0, "leverage": 0,
                "currency": "", "server": "", "account_name": "", "account_login": 0,
                "unrealized_pnl": 0, "daily_pnl": 0, "total_pnl": 0,
                "positions": [], "position_count": 0,
            }
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
            import MetaTrader5 as _mt5
            ti = _mt5.terminal_info()
            if ti is None:
                logger.debug("%s: terminal_info() returned None (headless terminal)", self._agent_id)
            else:
                logger.info("%s: status report trade_allowed=%s", self._agent_id, ti.trade_allowed)

            return {
                "type": "status",
                "connected": True,
                "agent_id": self._agent_id,
                "version": AGENT_VERSION,
                "hostname": self._hostname,
                "platform": self._platform,
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
                "trade_allowed": ti.trade_allowed if ti else None,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "daily_pnl": 0.0,
                "total_pnl": 0.0,
                "positions": pos_list,
                "position_count": len(pos_list),
            }
        except Exception:
            logger.exception("Failed to get MT5 status")
            return {
                "type": "status", "connected": False,
                "agent_id": self._agent_id,
                "version": AGENT_VERSION,
                "hostname": self._hostname,
                "platform": self._platform,
                "positions": [], "position_count": 0,
            }
        finally:
            executor.disconnect()


# ── Standalone MT5 helpers (used without FollowerExecutor context) ──

def get_mt5_account_info():
    import MetaTrader5 as mt5
    return mt5.account_info()


def mt5_positions_get():
    import MetaTrader5 as mt5
    return mt5.positions_get()
