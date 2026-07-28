"""Thread-safe shared state between bridge thread and asyncio server."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


class EquityHistory:
    """Ring buffer of equity/balance snapshots for the chart."""
    def __init__(self, max_points: int = 300):
        self.data: list[dict] = []
        self.max = max_points

    def record(self, timestamp: float, total_equity: float, total_balance: float) -> None:
        self.data.append({"t": timestamp, "e": total_equity, "b": total_balance})
        if len(self.data) > self.max:
            self.data = self.data[-self.max:]

    def get(self, limit: int = 200) -> list[dict]:
        return self.data[-limit:]


class ActivityLog:
    """Thread-safe ring buffer of recent events."""
    def __init__(self, max_entries: int = 500):
        self._lock = threading.Lock()
        self.entries: list[dict] = []
        self.max = max_entries

    def add(self, type_: str, message: str) -> None:
        with self._lock:
            self.entries.append({
                "t": time.time(),
                "type": type_,
                "msg": message,
            })
            if len(self.entries) > self.max:
                self.entries = self.entries[-self.max:]

    def get(self, limit: int = 100, type_filter: Optional[str] = None) -> list[dict]:
        with self._lock:
            if type_filter:
                filtered = [e for e in self.entries if e["type"] == type_filter]
                return filtered[-limit:]
            return self.entries[-limit:]


@dataclass
class AgentInfo:
    """Status of a connected follower agent."""
    name: str
    connected: bool = False
    ip: str = ""
    connected_at: float = 0.0
    last_seen: float = 0.0

    # Identity (set during registration)
    agent_id: str = ""           # unique persistent agent identifier
    version: str = ""            # software version
    hostname: str = ""           # machine hostname
    platform: str = ""           # OS platform

    # Account
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    leverage: int = 0
    currency: str = "USD"
    server: str = ""
    account_name: str = ""
    account_login: int = 0

    # PnL
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0

    # Positions
    positions: list[dict] = field(default_factory=list)
    position_count: int = 0

    # Connection
    latency_ms: float = 0.0
    last_event_time: float = 0.0
    events_copied: int = 0
    errors: int = 0
    ping_history: list[float] = field(default_factory=list)

    # Remote config overrides (pushed from dashboard)
    config_overrides: dict = field(default_factory=dict)

    @property
    def total_floating_pnl(self) -> float:
        return sum(p.get("profit", 0) + p.get("swap", 0) for p in self.positions)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "ip": self.ip,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "agent_id": self.agent_id,
            "version": self.version,
            "hostname": self.hostname,
            "platform": self.platform,
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "margin_free": self.margin_free,
            "margin_level": self.margin_level,
            "leverage": self.leverage,
            "currency": self.currency,
            "server": self.server,
            "account_name": self.account_name,
            "account_login": self.account_login,
            "unrealized_pnl": self.unrealized_pnl,
            "daily_pnl": self.daily_pnl,
            "total_pnl": self.total_pnl,
            "positions": list(self.positions),
            "position_count": self.position_count,
            "latency_ms": self.latency_ms,
            "last_event_time": self.last_event_time,
            "events_copied": self.events_copied,
            "errors": self.errors,
            "config_overrides": dict(self.config_overrides),
        }


@dataclass
class BridgeStats:
    cycles: int = 0
    events_detected: int = 0
    errors: int = 0
    last_event_time: float = 0.0
    start_time: float = 0.0
    master_connected: bool = False
    connected_agents: int = 0


class SharedState:
    """Thread-safe container for bridge state visible to dashboard + agents."""

    def __init__(self):
        self._lock = threading.Lock()
        self.master_positions: list[dict] = []
        self.master_account: dict = {}
        self.agents: dict[str, AgentInfo] = {}
        self.stats = BridgeStats()
        self.known_tickets: set[int] = set()
        self.equity_history = EquityHistory()
        self.activity = ActivityLog()
        self.follower_states: dict[str, dict] = {}

    # ── Master ────────────────────────────────────────────────

    def update_master(self, positions: list[dict], account: Optional[dict] = None) -> None:
        with self._lock:
            self.master_positions = positions
            if account:
                self.master_account = account

    def get_master(self) -> list[dict]:
        with self._lock:
            return list(self.master_positions)

    def get_master_account(self) -> dict:
        with self._lock:
            return dict(self.master_account)

    # ── Local Followers ────────────────────────────────────────

    def follower_states_snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self.follower_states.items()}

    def set_follower_active(self, name: str, active: bool) -> None:
        with self._lock:
            s = self.follower_states.setdefault(name, {})
            s["active"] = active
            s["name"] = name
            if not active:
                s["connected"] = False

    def register_follower_connection(self, name: str, login: int, server: str, balance: float, equity: float) -> None:
        with self._lock:
            s = self.follower_states.setdefault(name, {})
            s["name"] = name
            s["connected"] = True
            s["login"] = login
            s["server"] = server
            s["balance"] = balance
            s["equity"] = equity
            s["active_since"] = time.time()

    def record_follower_event(self, name: str, success: bool) -> None:
        with self._lock:
            s = self.follower_states.get(name)
            if not s:
                return
            s["events_total"] = s.get("events_total", 0) + 1
            s["events_ok"] = s.get("events_ok", 0) + (1 if success else 0)
            s["events_fail"] = s.get("events_fail", 0) + (0 if success else 1)

    def record_follower_error(self, name: str) -> None:
        with self._lock:
            s = self.follower_states.get(name)
            if s:
                s["errors"] = s.get("errors", 0) + 1

    # ── Agents ────────────────────────────────────────────────

    def register_agent(self, name: str, ip: str) -> AgentInfo:
        with self._lock:
            info = self.agents.get(name)
            if info is None:
                info = AgentInfo(name=name)
                self.agents[name] = info
            info.connected = True
            info.ip = ip
            info.connected_at = time.time()
            info.last_seen = time.time()
            return info

    def update_agent_info(self, name: str, data: dict) -> None:
        """Update identity/info fields from registration message."""
        with self._lock:
            info = self.agents.get(name)
            if not info:
                return
            info.agent_id = data.get("agent_id", info.agent_id)
            info.version = data.get("version", info.version)
            info.hostname = data.get("hostname", info.hostname)
            info.platform = data.get("platform", info.platform)
            config = data.get("config_overrides")
            if config and isinstance(config, dict):
                info.config_overrides.update(config)

    def unregister_agent(self, name: str) -> None:
        with self._lock:
            info = self.agents.get(name)
            if info:
                info.connected = False
                info.ip = ""

    def get_agent_config_override(self, name: str) -> dict:
        with self._lock:
            info = self.agents.get(name)
            if not info:
                return {}
            return dict(info.config_overrides)

    def set_agent_config_override(self, name: str, config: dict) -> None:
        with self._lock:
            info = self.agents.get(name)
            if not info:
                return
            info.config_overrides.update(config)

    def update_agent_status(self, name: str, data: dict) -> None:
        with self._lock:
            info = self.agents.get(name)
            if not info:
                return
            info.last_seen = time.time()
            info.balance = data.get("balance", info.balance)
            info.equity = data.get("equity", info.equity)
            info.margin = data.get("margin", info.margin)
            info.margin_free = data.get("margin_free", info.margin_free)
            info.margin_level = data.get("margin_level", info.margin_level)
            info.leverage = data.get("leverage", info.leverage)
            info.currency = data.get("currency", info.currency)
            info.server = data.get("server", info.server)
            info.account_name = data.get("account_name", info.account_name)
            info.account_login = data.get("account_login", info.account_login)
            info.unrealized_pnl = data.get("unrealized_pnl", info.unrealized_pnl)
            info.daily_pnl = data.get("daily_pnl", info.daily_pnl)
            info.total_pnl = data.get("total_pnl", info.total_pnl)
            info.positions = data.get("positions", info.positions)
            info.position_count = data.get("position_count", len(info.positions))
            # Also update identity fields if present
            for f in ("agent_id", "version", "hostname", "platform"):
                val = data.get(f)
                if val:
                    setattr(info, f, val)

    def record_agent_event(self, name: str, success: bool) -> None:
        with self._lock:
            info = self.agents.get(name)
            if not info:
                return
            info.last_event_time = time.time()
            info.events_copied += 1
            if not success:
                info.errors += 1

    def record_activity(self, type_: str, message: str) -> None:
        self.activity.add(type_, message)

    def get_activity(self, limit: int = 100, type_filter: Optional[str] = None) -> list[dict]:
        return self.activity.get(limit, type_filter)

    def record_agent_latency(self, name: str, latency_ms: float) -> None:
        with self._lock:
            info = self.agents.get(name)
            if not info:
                return
            info.latency_ms = latency_ms
            info.ping_history.append(latency_ms)
            if len(info.ping_history) > 60:
                info.ping_history = info.ping_history[-60:]

    def get_agents(self) -> dict[str, AgentInfo]:
        with self._lock:
            return {k: v for k, v in self.agents.items()}

    def get_connected_agent_names(self) -> list[str]:
        with self._lock:
            return [k for k, v in self.agents.items() if v.connected]

    def get_agent(self, name: str) -> Optional[AgentInfo]:
        with self._lock:
            return self.agents.get(name)

    # ── Stats ─────────────────────────────────────────────────

    def update_stats(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.stats, k):
                    setattr(self.stats, k, v)

    def get_stats(self) -> dict:
        with self._lock:
            self.stats.connected_agents = sum(1 for a in self.agents.values() if a.connected)
            return {
                "cycles": self.stats.cycles,
                "events_detected": self.stats.events_detected,
                "errors": self.stats.errors,
                "last_event_time": self.stats.last_event_time,
                "start_time": self.stats.start_time,
                "master_connected": self.stats.master_connected,
                "connected_agents": self.stats.connected_agents,
                "known_tickets": len(self.known_tickets),
                "uptime": time.time() - self.stats.start_time if self.stats.start_time else 0,
            }

    # ── Full snapshot for dashboard ──────────────────────────

    def snapshot(self) -> dict:
        """Atomic snapshot of everything for the dashboard."""
        with self._lock:
            # Compute portfolio totals
            total_balance = self.master_account.get("balance", 0)
            total_equity = self.master_account.get("equity", 0)
            total_margin = self.master_account.get("margin", 0)
            total_floating = sum(p.get("profit", 0) + p.get("swap", 0) for p in self.master_positions)
            total_positions = len(self.master_positions)

            agents_data = {}
            for k, v in self.agents.items():
                d = v.to_dict()
                total_balance += v.balance
                total_equity += v.equity
                total_margin += v.margin
                total_floating += v.total_floating_pnl
                total_positions += v.position_count
                agents_data[k] = d

            return {
                "master": {
                    "positions": list(self.master_positions),
                    "account": dict(self.master_account),
                },
                "agents": agents_data,
                "portfolio": {
                    "total_balance": total_balance,
                    "total_equity": total_equity,
                    "total_margin": total_margin,
                    "total_margin_free": total_equity - total_margin,
                    "total_floating_pnl": round(total_floating, 2),
                    "total_positions": total_positions,
                    "total_agents": len(agents_data),
                    "connected_agents": sum(1 for a in agents_data.values() if a["connected"]),
                },
                "stats": {
                    "cycles": self.stats.cycles,
                    "events_detected": self.stats.events_detected,
                    "errors": self.stats.errors,
                    "last_event_time": self.stats.last_event_time,
                    "start_time": self.stats.start_time,
                    "connected_agents": sum(1 for a in agents_data.values() if a["connected"]),
                    "known_tickets": len(self.known_tickets),
                    "uptime": time.time() - self.stats.start_time if self.stats.start_time else 0,
                },
            }
