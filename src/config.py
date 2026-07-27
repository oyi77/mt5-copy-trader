"""Configuration loader and serializer for copy-trade bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import yaml


@dataclass
class MasterConfig:
    path: str = "C:/Program Files/MetaTrader 5/terminal64.exe"
    port: int = 15555


@dataclass
class FollowerConfig:
    name: str
    path: str
    port: int
    login: int
    password: str
    server: str
    lot_multiplier: float = 1.0
    max_lot: float = 10.0
    min_lot: float = 0.01
    max_positions: int = 20
    deviation: int = 20
    magic: int = 951001
    symbol_mapping: dict[str, str] = field(default_factory=dict)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/copy_trade.log"
    max_size_mb: int = 50
    backup_count: int = 3


@dataclass
class Config:
    master: MasterConfig = field(default_factory=MasterConfig)
    followers: list[FollowerConfig] = field(default_factory=list)
    poll_interval_ms: int = 300
    host: str = "0.0.0.0"
    port: int = 5000
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def update_master(self, data: dict) -> None:
        if "path" in data:
            self.master.path = data["path"]
        if "port" in data:
            self.master.port = int(data["port"])

    def update_server(self, data: dict) -> None:
        if "host" in data:
            self.host = data["host"]
        if "port" in data:
            self.port = int(data["port"])
        if "poll_interval_ms" in data:
            self.poll_interval_ms = int(data["poll_interval_ms"])

    def add_follower(self, data: dict) -> FollowerConfig:
        f = FollowerConfig(
            name=data["name"],
            path=data.get("path", self.master.path),
            port=int(data.get("port", 15556)),
            login=int(data.get("login", 0)),
            password=data.get("password", ""),
            server=data.get("server", ""),
            lot_multiplier=float(data.get("lot_multiplier", 1.0)),
            max_lot=float(data.get("max_lot", 10.0)),
            min_lot=float(data.get("min_lot", 0.01)),
            max_positions=int(data.get("max_positions", 20)),
            deviation=int(data.get("deviation", 20)),
            magic=int(data.get("magic", 951001)),
            symbol_mapping={k.upper(): v for k, v in data.get("symbol_mapping", {}).items()},
        )
        self.followers.append(f)
        return f

    def update_follower(self, name: str, data: dict) -> Optional[FollowerConfig]:
        for f in self.followers:
            if f.name == name:
                if "name" in data:
                    f.name = data["name"]
                if "path" in data:
                    f.path = data["path"]
                if "port" in data:
                    f.port = int(data["port"])
                if "login" in data:
                    f.login = int(data["login"])
                if "password" in data:
                    f.password = data["password"]
                if "server" in data:
                    f.server = data["server"]
                if "lot_multiplier" in data:
                    f.lot_multiplier = float(data["lot_multiplier"])
                if "max_lot" in data:
                    f.max_lot = float(data["max_lot"])
                if "min_lot" in data:
                    f.min_lot = float(data["min_lot"])
                if "max_positions" in data:
                    f.max_positions = int(data["max_positions"])
                if "deviation" in data:
                    f.deviation = int(data["deviation"])
                if "magic" in data:
                    f.magic = int(data["magic"])
                if "symbol_mapping" in data:
                    f.symbol_mapping = {k.upper(): v for k, v in data["symbol_mapping"].items()}
                return f
        return None

    def remove_follower(self, name: str) -> bool:
        before = len(self.followers)
        self.followers = [f for f in self.followers if f.name != name]
        return len(self.followers) < before


@dataclass
class AgentConfig:
    """Configuration for a follower agent (runs on remote machine)."""
    name: str
    hub_url: str
    mt5_path: str
    mt5_port: int
    mt5_login: int
    mt5_password: str
    mt5_server: str
    lot_multiplier: float = 1.0
    max_lot: float = 10.0
    min_lot: float = 0.01
    max_positions: int = 20
    deviation: int = 20
    magic: int = 951001
    symbol_mapping: dict[str, str] = field(default_factory=dict)
    log_file: str = "logs/agent.log"
    log_level: str = "INFO"


# ── Serialization ─────────────────────────────────────────────

def follower_to_safe_dict(f: FollowerConfig) -> dict:
    """Follower dict without password (safe for API response)."""
    return {
        "name": f.name,
        "path": f.path,
        "port": f.port,
        "login": f.login,
        "has_password": bool(f.password),
        "server": f.server,
        "lot_multiplier": f.lot_multiplier,
        "max_lot": f.max_lot,
        "min_lot": f.min_lot,
        "max_positions": f.max_positions,
        "deviation": f.deviation,
        "magic": f.magic,
        "symbol_mapping": dict(f.symbol_mapping),
    }


def follower_to_full_dict(f: FollowerConfig) -> dict:
    """Follower dict WITH password (for config export/download)."""
    d = follower_to_safe_dict(f)
    d["password"] = f.password
    del d["has_password"]
    return d


def config_to_dict(cfg: Config) -> dict:
    """Serialize entire Config to a dict for YAML output."""
    return {
        "master": {
            "path": cfg.master.path,
            "port": cfg.master.port,
        },
        "followers": [follower_to_full_dict(f) for f in cfg.followers],
        "server": {
            "host": cfg.host,
            "port": cfg.port,
        },
        "poll_interval_ms": cfg.poll_interval_ms,
        "logging": {
            "level": cfg.logging.level,
            "file": cfg.logging.file,
            "max_size_mb": cfg.logging.max_size_mb,
            "backup_count": cfg.logging.backup_count,
        },
    }


def agent_config_to_yaml(cfg: Config, follower_name: str, hub_url: str) -> str:
    """Generate agent_config.yaml content for a remote follower."""
    f = next((f for f in cfg.followers if f.name == follower_name), None)
    if not f:
        raise ValueError(f"Follower '{follower_name}' not found")

    return f"""# Agent config generated by Copy Trade Engine dashboard
# Copy this to the follower machine and run: python agent.py {follower_name}_agent.yaml

name: "{f.name}"

# Hub connection (master machine running run.py)
hub_url: "{hub_url}"

# Local MT5 terminal
mt5_path: "{f.path}"
mt5_port: {f.port}
mt5_login: {f.login}
mt5_password: "{f.password}"
mt5_server: "{f.server}"

# Lot scaling
lot_multiplier: {f.lot_multiplier}
max_lot: {f.max_lot}
min_lot: {f.min_lot}
max_positions: {f.max_positions}
deviation: {f.deviation}
magic: {f.magic}

# Symbol mapping
symbol_mapping:
{chr(10).join(f'  "{k}": "{v}"' for k, v in f.symbol_mapping.items()) if f.symbol_mapping else "  # none"}

log_file: "logs/agent.log"
log_level: "INFO"
"""


# ── File I/O ──────────────────────────────────────────────────

def load_config(path: str) -> Config:
    """Load and validate config from YAML file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    cfg = Config()

    # Master
    m = raw.get("master", {})
    if isinstance(m, dict):
        cfg.master.path = m.get("path", cfg.master.path)
        cfg.master.port = m.get("port", cfg.master.port)

    # Followers (local — same-machine)
    for i, f_data in enumerate(raw.get("followers", [])):
        if not isinstance(f_data, dict):
            continue
        cfg.followers.append(FollowerConfig(
            name=f_data.get("name", f"Follower_{i+1}"),
            path=f_data.get("path", cfg.master.path),
            port=int(f_data.get("port", 15556 + i)),
            login=int(f_data.get("login", 0)),
            password=f_data.get("password", ""),
            server=f_data.get("server", ""),
            lot_multiplier=float(f_data.get("lot_multiplier", 1.0)),
            max_lot=float(f_data.get("max_lot", 10.0)),
            min_lot=float(f_data.get("min_lot", 0.01)),
            max_positions=int(f_data.get("max_positions", 20)),
            deviation=int(f_data.get("deviation", 20)),
            magic=int(f_data.get("magic", cfg.master.port + i)),
            symbol_mapping={k.upper(): v for k, v in f_data.get("symbol_mapping", {}).items()},
        ))

    # Poll interval
    cfg.poll_interval_ms = int(raw.get("poll_interval_ms", cfg.poll_interval_ms))

    # Dashboard / hub server
    server = raw.get("server", {})
    if isinstance(server, dict):
        cfg.host = server.get("host", cfg.host)
        cfg.port = int(server.get("port", cfg.port))

    # Logging
    lc = raw.get("logging", {})
    if isinstance(lc, dict):
        cfg.logging.level = lc.get("level", cfg.logging.level)
        cfg.logging.file = lc.get("file", cfg.logging.file)
        cfg.logging.max_size_mb = int(lc.get("max_size_mb", cfg.logging.max_size_mb))
        cfg.logging.backup_count = int(lc.get("backup_count", cfg.logging.backup_count))

    # Validate
    if not cfg.master.path:
        raise ValueError("master.path is required")

    return cfg


def save_config(cfg: Config, path: str) -> None:
    """Write Config back to YAML file, preserving existing comments."""
    d = config_to_dict(cfg)
    # Remove empty follower list so YAML shows `followers: []`
    if not d["followers"]:
        d["followers"] = []
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(d, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def load_agent_config(path: str) -> AgentConfig:
    """Load agent config from YAML file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Agent config not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    cfg = AgentConfig(
        name=raw.get("name", "agent"),
        hub_url=raw.get("hub_url", "http://localhost:5000"),
        mt5_path=raw.get("mt5_path", "C:/Program Files/MetaTrader 5/terminal64.exe"),
        mt5_port=int(raw.get("mt5_port", 15555)),
        mt5_login=int(raw.get("mt5_login", 0)),
        mt5_password=raw.get("mt5_password", ""),
        mt5_server=raw.get("mt5_server", ""),
        lot_multiplier=float(raw.get("lot_multiplier", 1.0)),
        max_lot=float(raw.get("max_lot", 10.0)),
        min_lot=float(raw.get("min_lot", 0.01)),
        max_positions=int(raw.get("max_positions", 20)),
        deviation=int(raw.get("deviation", 20)),
        magic=int(raw.get("magic", 951001)),
        symbol_mapping={k.upper(): v for k, v in raw.get("symbol_mapping", {}).items()},
        log_file=raw.get("log_file", "logs/agent.log"),
        log_level=raw.get("log_level", "INFO"),
    )

    if not cfg.mt5_login:
        raise ValueError("Agent config missing mt5_login")
    if not cfg.mt5_password:
        raise ValueError("Agent config missing mt5_password")
    if not cfg.mt5_server:
        raise ValueError("Agent config missing mt5_server")

    return cfg
