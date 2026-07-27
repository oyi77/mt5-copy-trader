"""aiohttp server: dashboard (REST + SSE) + agent WebSocket hub + config API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Optional

import aiohttp
from aiohttp import web

from src.config import (
    Config,
    follower_to_safe_dict,
    agent_config_to_yaml,
    save_config,
)
from src.state import SharedState

logger = logging.getLogger(__name__)

_FROZEN = getattr(sys, 'frozen', False)
if _FROZEN:
    HERE = sys._MEIPASS
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
# When running from source, HERE = src/, so STATIC = src/static.
# Fall back to parent directory (project root).
if not os.path.isdir(STATIC):
    parent = os.path.dirname(HERE)
    candidate = os.path.join(parent, "static")
    if os.path.isdir(candidate):
        STATIC = candidate


# ──────────────────────────────────────────────────────────────
# Agent WebSocket hub
# ──────────────────────────────────────────────────────────────

class AgentHub:
    """Manages WebSocket connections from remote follower agents."""

    def __init__(self, state: SharedState, event_queue: asyncio.Queue):
        self._state = state
        self._event_queue = event_queue
        self._connections: dict[str, web.WebSocketResponse] = {}
        self._broadcast_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop(self) -> None:
        if self._broadcast_task:
            self._broadcast_task.cancel()
        if self._ping_task:
            self._ping_task.cancel()

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=256 * 1024)
        await ws.prepare(request)

        name = request.query.get("name", f"agent-{id(ws):x}")
        ip = request.remote or "unknown"
        self._connections[name] = ws
        self._state.register_agent(name, ip)
        logger.info("Agent connected: %s from %s", name, ip)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("Agent %s: invalid JSON", name)
                        continue

                    msg_type = data.get("type", "")
                    if msg_type == "status":
                        self._state.update_agent_status(name, data)
                    elif msg_type == "execution_result":
                        success = data.get("success", False)
                        self._state.record_agent_event(name, success)
                        logger.info(
                            "Agent %s: %s %s (master_ticket=%s)",
                            name,
                            "OK" if success else "FAIL",
                            data.get("action", "?"),
                            data.get("master_ticket", "?"),
                        )
                    elif msg_type == "pong":
                        sent = data.get("timestamp", 0)
                        if sent:
                            latency = round((time.time() - sent) * 1000, 1)
                            self._state.record_agent_latency(name, latency)

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("Agent %s WS error: %s", name, ws.exception())

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Agent %s: unexpected error", name)
        finally:
            self._connections.pop(name, None)
            self._state.unregister_agent(name)
            logger.info("Agent disconnected: %s", name)

        return ws

    def agents_to_broadcast(self) -> list[tuple[str, web.WebSocketResponse]]:
        result = []
        dead = []
        for name, ws in self._connections.items():
            if ws.closed:
                dead.append(name)
            else:
                result.append((name, ws))
        for name in dead:
            self._connections.pop(name, None)
            self._state.unregister_agent(name)
        return result

    async def get_agent_ws(self, name: str) -> Optional[web.WebSocketResponse]:
        """Get WebSocket for a specific connected agent."""
        ws = self._connections.get(name)
        if ws and not ws.closed:
            return ws
        return None

    async def send_to_agent(self, name: str, data: dict) -> bool:
        """Send a JSON message to a specific agent. Returns True on success."""
        ws = await self.get_agent_ws(name)
        if not ws:
            return False
        try:
            await ws.send_str(json.dumps(data))
            return True
        except (ConnectionError, asyncio.TimeoutError):
            self._connections.pop(name, None)
            self._state.unregister_agent(name)
            return False

    async def _broadcast_loop(self) -> None:
        while True:
            try:
                events = await self._event_queue.get()
                if not events:
                    continue

                for event in events:
                    payload = json.dumps({"type": "trade", "event": event})
                    agents = self.agents_to_broadcast()
                    for name, ws in agents:
                        try:
                            await ws.send_str(payload)
                        except (ConnectionError, asyncio.TimeoutError):
                            self._connections.pop(name, None)
                            self._state.unregister_agent(name)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Broadcast loop error")
                await asyncio.sleep(1)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            agents = self.agents_to_broadcast()
            for name, ws in agents:
                try:
                    await ws.send_str(json.dumps({"type": "ping", "timestamp": now}))
                except (ConnectionError, asyncio.TimeoutError):
                    pass


# ──────────────────────────────────────────────────────────────
# Dashboard REST + SSE
# ──────────────────────────────────────────────────────────────

def _get_agent_order(state: SharedState) -> list[dict]:
    snapshot = state.snapshot()
    rows = []

    # Master
    master_acc = snapshot["master"]["account"]
    rows.append({
        "id": "__master__",
        "name": "MASTER",
        "type": "master",
        "connected": True,
        "balance": master_acc.get("balance", 0),
        "equity": master_acc.get("equity", 0),
        "margin": master_acc.get("margin", 0),
        "margin_free": master_acc.get("margin_free", 0),
        "margin_level": master_acc.get("margin_level", 0),
        "leverage": master_acc.get("leverage", 0),
        "currency": master_acc.get("currency", "USD"),
        "server": master_acc.get("server", ""),
        "account_name": master_acc.get("name", ""),
        "account_login": master_acc.get("login", 0),
        "unrealized_pnl": sum(p.get("profit", 0) + p.get("swap", 0) for p in snapshot["master"]["positions"]),
        "daily_pnl": 0,
        "positions": snapshot["master"]["positions"],
        "position_count": len(snapshot["master"]["positions"]),
        "latency_ms": 0,
        "last_seen": time.time(),
        "events_copied": 0,
        "errors": 0,
    })

    # Agents
    agents = sorted(
        snapshot["agents"].values(),
        key=lambda a: (not a["connected"], a["name"]),
    )
    for a in agents:
        rows.append({
            "id": a["name"],
            "name": a["name"],
            "type": "agent",
            "connected": a["connected"],
            "balance": a["balance"],
            "equity": a["equity"],
            "margin": a["margin"],
            "margin_free": a["margin_free"],
            "margin_level": a["margin_level"],
            "leverage": a["leverage"],
            "currency": a["currency"],
            "server": a["server"],
            "account_name": a["account_name"],
            "account_login": a["account_login"],
            "unrealized_pnl": a["unrealized_pnl"],
            "daily_pnl": a["daily_pnl"],
            "positions": a["positions"],
            "position_count": a["position_count"],
            "latency_ms": a["latency_ms"],
            "last_seen": a["last_seen"],
            "events_copied": a["events_copied"],
            "errors": a["errors"],
            "ping_history": a.get("ping_history", []),
        })
    return rows


async def handle_api_status(request: web.Request) -> web.Response:
    state: SharedState = request.app["state"]
    rows = _get_agent_order(state)
    snap = state.snapshot()
    return web.json_response({
        "accounts": rows,
        "portfolio": snap["portfolio"],
        "stats": snap["stats"],
        "t": time.time(),
    })


async def handle_api_stream(request: web.Request) -> web.StreamResponse:
    state: SharedState = request.app["state"]
    response = web.StreamResponse(
        status=200, reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    try:
        while True:
            rows = _get_agent_order(state)
            snap = state.snapshot()
            payload = json.dumps({
                "accounts": rows,
                "portfolio": snap["portfolio"],
                "stats": snap["stats"],
                "t": time.time(),
            })
            await response.write(f"data: {payload}\n\n".encode())
            await asyncio.sleep(2)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    return response


async def handle_favicon(request: web.Request) -> web.Response:
    """Serve an inline SVG favicon."""
    import io
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="%236366f1"/><text x="32" y="44" text-anchor="middle" fill="white" font-size="36" font-weight="bold" font-family="sans-serif">C</text></svg>'
    return web.Response(
        body=svg,
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(STATIC, "index.html"))


# ──────────────────────────────────────────────────────────────
# Portfolio API
# ──────────────────────────────────────────────────────────────

async def handle_api_portfolio(request: web.Request) -> web.Response:
    state: SharedState = request.app["state"]
    snap = state.snapshot()
    return web.json_response({
        "portfolio": snap["portfolio"],
        "accounts": _get_agent_order(state),
        "t": time.time(),
    })


# ──────────────────────────────────────────────────────────────
# Agent management API
# ──────────────────────────────────────────────────────────────

async def handle_list_agents(request: web.Request) -> web.Response:
    state: SharedState = request.app["state"]
    agents = state.get_agents()
    return web.json_response({
        "agents": {k: v.to_dict() for k, v in agents.items()},
    })


async def handle_get_agent(request: web.Request) -> web.Response:
    state: SharedState = request.app["state"]
    name = request.match_info["name"]
    agent = state.get_agent(name)
    if not agent:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(agent.to_dict())


async def handle_ping_agent(request: web.Request) -> web.Response:
    """Send explicit ping to an agent and wait for pong."""
    state: SharedState = request.app["state"]
    hub: AgentHub = request.app["hub"]
    name = request.match_info["name"]

    agent = state.get_agent(name)
    if not agent or not agent.connected:
        return web.json_response({"error": "agent not connected"}, status=400)

    sent_ts = time.time()
    ok = await hub.send_to_agent(name, {"type": "ping", "timestamp": sent_ts})
    if not ok:
        return web.json_response({"error": "send failed"}, status=502)

    # Wait for pong (agent will update latency via status update on next cycle)
    await asyncio.sleep(1.0)
    agent = state.get_agent(name)
    return web.json_response({
        "latency_ms": agent.latency_ms if agent else -1,
        "connected": agent.connected if agent else False,
    })


# ──────────────────────────────────────────────────────────────
# Activity + Equity History API
# ──────────────────────────────────────────────────────────────

async def handle_activity(request: web.Request) -> web.Response:
    state: SharedState = request.app["state"]
    limit = int(request.query.get("limit", 100))
    type_filter = request.query.get("type", None)
    return web.json_response({
        "events": state.get_activity(limit, type_filter),
        "t": time.time(),
    })


async def handle_equity_history(request: web.Request) -> web.Response:
    state: SharedState = request.app["state"]
    limit = int(request.query.get("limit", 200))
    return web.json_response({
        "points": state.equity_history.get(limit),
        "t": time.time(),
    })


async def handle_config_backup(request: web.Request) -> web.Response:
    """Download a backup of the current config."""
    cfg: Config = request.app["config"]
    from src.config import config_to_dict
    import yaml
    d = config_to_dict(cfg)
    yaml_text = yaml.dump(d, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return web.Response(
        text=yaml_text,
        content_type="text/yaml",
        headers={"Content-Disposition": 'attachment; filename="config_backup.yaml"'},
    )


async def handle_config_restore(request: web.Request) -> web.Response:
    """Restore config from uploaded backup."""
    import yaml
    try:
        data = await request.json()
        yaml_text = data.get("config", "")
        if not yaml_text:
            return web.json_response({"error": "empty config"}, status=400)
        raw = yaml.safe_load(yaml_text)
        if not isinstance(raw, dict):
            return web.json_response({"error": "invalid yaml"}, status=400)

        cfg: Config = request.app["config"]
        from src.config import load_config, save_config

        # Apply to the running config
        if "master" in raw:
            cfg.update_master(raw["master"])
        if "server" in raw:
            cfg.update_server(raw["server"])
        if "followers" in raw:
            cfg.followers.clear()
            for f_data in raw["followers"]:
                if isinstance(f_data, dict):
                    cfg.add_follower(f_data)
        if "poll_interval_ms" in raw:
            cfg.poll_interval_ms = int(raw["poll_interval_ms"])

        save_config(cfg, request.app["config_path"])
        return web.json_response({"status": "ok", "message": "Config restored. Restart bridge to apply."})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


# ──────────────────────────────────────────────────────────────
# Config management API
# ──────────────────────────────────────────────────────────────

async def handle_get_config(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    return web.json_response({
        "master": {"path": cfg.master.path, "port": cfg.master.port},
        "server": {"host": cfg.host, "port": cfg.port},
        "poll_interval_ms": cfg.poll_interval_ms,
        "followers": [follower_to_safe_dict(f) for f in cfg.followers],
        "config_path": request.app.get("config_path", ""),
    })


async def handle_update_master(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    data = await request.json()
    cfg.update_master(data)
    save_config(cfg, request.app["config_path"])
    return web.json_response({"status": "ok"})


async def handle_update_server(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    data = await request.json()
    cfg.update_server(data)
    save_config(cfg, request.app["config_path"])
    return web.json_response({"status": "ok"})


async def handle_add_follower(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    data = await request.json()
    if not data.get("name"):
        return web.json_response({"status": "error", "message": "name required"}, status=400)
    if any(f.name == data["name"] for f in cfg.followers):
        return web.json_response({"status": "error", "message": "name already exists"}, status=409)
    f = cfg.add_follower(data)
    save_config(cfg, request.app["config_path"])
    return web.json_response({"status": "ok", "follower": follower_to_safe_dict(f)})


async def handle_update_follower(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    name = request.match_info["name"]
    data = await request.json()
    f = cfg.update_follower(name, data)
    if not f:
        return web.json_response({"status": "error", "message": "not found"}, status=404)
    save_config(cfg, request.app["config_path"])
    return web.json_response({"status": "ok", "follower": follower_to_safe_dict(f)})


async def handle_delete_follower(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    name = request.match_info["name"]
    if cfg.remove_follower(name):
        save_config(cfg, request.app["config_path"])
        return web.json_response({"status": "ok"})
    return web.json_response({"status": "error", "message": "not found"}, status=404)


async def handle_export_agent_config(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    name = request.query.get("name", "")
    if not name:
        return web.json_response({"status": "error", "message": "?name= required"}, status=400)
    try:
        hub_url = f"http://{request.host}"
        yaml_text = agent_config_to_yaml(cfg, name, hub_url)
        return web.Response(
            text=yaml_text,
            content_type="text/yaml",
            headers={"Content-Disposition": f'attachment; filename="{name}_agent.yaml"'},
        )
    except ValueError as e:
        return web.json_response({"status": "error", "message": str(e)}, status=404)


# ──────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────

def create_app(
    state: SharedState,
    event_queue: asyncio.Queue,
    cfg: Config,
    config_path: str = "config.yaml",
) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["event_queue"] = event_queue
    app["config"] = cfg
    app["config_path"] = config_path
    app['public_config_path'] = config_path

    hub = AgentHub(state, event_queue)
    app["hub"] = hub

    # Dashboard
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_get("/", handle_index)
    app.router.add_static("/static", STATIC, name="static")
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/stream", handle_api_stream)
    app.router.add_get("/api/portfolio", handle_api_portfolio)

    # Agent WebSocket hub
    app.router.add_get("/ws/agent", hub.handle_ws)

    # Agent management
    app.router.add_get("/api/agents", handle_list_agents)
    app.router.add_get("/api/agents/{name}", handle_get_agent)
    app.router.add_post("/api/agents/{name}/ping", handle_ping_agent)

    # Activity + Equity
    app.router.add_get("/api/activity", handle_activity)
    app.router.add_get("/api/equity-history", handle_equity_history)

    # Config backup/restore
    app.router.add_get("/api/config/backup", handle_config_backup)
    app.router.add_post("/api/config/restore", handle_config_restore)

    # Config management
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_put("/api/config/master", handle_update_master)
    app.router.add_put("/api/config/server", handle_update_server)
    app.router.add_post("/api/config/followers", handle_add_follower)
    app.router.add_put("/api/config/followers/{name}", handle_update_follower)
    app.router.add_delete("/api/config/followers/{name}", handle_delete_follower)
    app.router.add_get("/api/config/export-agent", handle_export_agent_config)

    async def _record_equity(app):
        """Background task: record equity snapshots every 60s."""
        state: SharedState = app["state"]
        while True:
            try:
                await asyncio.sleep(60)
                snap = state.snapshot()
                state.equity_history.record(
                    time.time(),
                    snap["portfolio"]["total_equity"],
                    snap["portfolio"]["total_balance"],
                )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _on_startup(app):
        hub.start()
        asyncio.create_task(_record_equity(app))

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(lambda _: hub.stop())

    return app
