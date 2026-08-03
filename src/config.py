"""Configuration loader and serializer for copy-trade bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Optional

import yaml


@dataclass
class MasterConfig:
    path: str = "C:/Program Files/MetaTrader 5/terminal64.exe"
    port: int = 15555
    # When set, the master is polled through TradeSender.mq5's signal file
    # instead of the MetaTrader5 package / IPC (which some Exness builds
    # reject with -6). Must be the absolute path to MQL5\Files\master_signals.txt.
    ea_signals_file: str = ""
    # Optional EA-mode auto-recovery: when the signal file goes stale the
    # bridge gracefully closes + relaunches the master terminal and, if the
    # EA does not come back, runs ea_watchdog_attach_script to re-attach it.
    ea_watchdog: bool = False
    # Optional .ps1 that re-attaches TradeSender to a chart (UI automation).
    # Used only by the EA watchdog as a fallback after a terminal relaunch.
    ea_watchdog_attach_script: str = ""
    # Optional login credentials used ONLY by the EA watchdog when it
    # relaunches the master terminal (MT5 /login: CLI switch). The IPC poll
    # path never uses these.
    login: int = 0
    password: str = ""
    server: str = ""


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
    skip_own_magic: bool = True  # skip re-broadcast of trades this follower itself placed
    symbol_mapping: dict[str, str] = field(default_factory=dict)
    skip_auto_trading: bool = False
    dry_run: bool = False
    terminal_data_path: str = ""  # MQL5/Files dir for file-based execution (Exness)
    max_daily_loss: float = 0.0  # 0 = no limit
    max_drawdown_pct: float = 0.0  # 0 = no limit (e.g. 20 = 20% drawdown)
    max_daily_trades: int = 0  # 0 = no limit
    queue_path: str = "trade_queue.json"  # disk path for local event queue


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
            port = int(data["port"])
            _require_port(port, "master.port")
            self.master.port = port
        if "ea_signals_file" in data:
            self.master.ea_signals_file = data["ea_signals_file"]
        if "ea_watchdog" in data:
            self.master.ea_watchdog = bool(data["ea_watchdog"])
        if "ea_watchdog_attach_script" in data:
            self.master.ea_watchdog_attach_script = data["ea_watchdog_attach_script"]
        if "login" in data:
            self.master.login = int(data["login"])
        if "password" in data:
            self.master.password = data["password"]
        if "server" in data:
            self.master.server = data["server"]

    def update_server(self, data: dict) -> None:
        if "host" in data:
            self.host = data["host"]
        if "port" in data:
            port = int(data["port"])
            _require_port(port, "server.port")
            self.port = port
        if "poll_interval_ms" in data:
            poll_interval_ms = int(data["poll_interval_ms"])
            if poll_interval_ms < 100:
                raise ValueError(
                    f"poll_interval_ms must be an integer >= 100, got {poll_interval_ms!r}"
                )
            self.poll_interval_ms = poll_interval_ms

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
            skip_own_magic=data.get("skip_own_magic", True),
            symbol_mapping={k.upper(): v for k, v in data.get("symbol_mapping", {}).items()},
            skip_auto_trading=data.get("skip_auto_trading", False),
            dry_run=data.get("dry_run", False),
            terminal_data_path=data.get("terminal_data_path", ""),
            max_daily_loss=float(data.get("max_daily_loss", 0.0)),
            max_drawdown_pct=float(data.get("max_drawdown_pct", 0.0)),
            max_daily_trades=int(data.get("max_daily_trades", 0)),
        )
        self.followers.append(f)
        try:
            self.validate()
        except ValueError:
            self.followers.pop()
            raise
        return f

    def update_follower(self, name: str, data: dict) -> Optional[FollowerConfig]:
        for f in self.followers:
            if f.name == name:
                backup = {fl.name: getattr(f, fl.name) for fl in fields(f)}
                try:
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
                    if "skip_own_magic" in data:
                        f.skip_own_magic = bool(data["skip_own_magic"])
                    if "symbol_mapping" in data:
                        f.symbol_mapping = {k.upper(): v for k, v in (data["symbol_mapping"] or {}).items()}
                    if "terminal_data_path" in data:
                        f.terminal_data_path = data["terminal_data_path"]
                    if "dry_run" in data:
                        f.dry_run = bool(data["dry_run"])
                    if "max_daily_loss" in data:
                        f.max_daily_loss = float(data["max_daily_loss"])
                    if "max_drawdown_pct" in data:
                        f.max_drawdown_pct = float(data["max_drawdown_pct"])
                    if "max_daily_trades" in data:
                        f.max_daily_trades = int(data["max_daily_trades"])
                    if "queue_path" in data:
                        f.queue_path = data["queue_path"]
                    self.validate()
                except ValueError:
                    for fl in fields(f):
                        setattr(f, fl.name, backup[fl.name])
                    raise
                return f
        return None

    def remove_follower(self, name: str) -> bool:
        before = len(self.followers)
        self.followers = [f for f in self.followers if f.name != name]
        return len(self.followers) < before

    def validate(self) -> None:
        """Validate the whole config; raise ValueError with actionable messages."""
        _require_port(self.master.port, "master.port")
        _require_port(self.port, "server.port")
        if isinstance(self.poll_interval_ms, bool) or not isinstance(self.poll_interval_ms, int) or self.poll_interval_ms < 100:
            raise ValueError(
                f"poll_interval_ms must be an integer >= 100, got {self.poll_interval_ms!r}"
            )
        seen: set[str] = set()
        for f in self.followers:
            _validate_follower(f)
            if f.name in seen:
                raise ValueError(
                    f"Duplicate follower name: '{f.name}' — every follower needs a unique name"
                )
            seen.add(f.name)


# ── Validation helpers ────────────────────────────────────────

def _require_port(value: Any, label: str) -> None:
    """Require an integer TCP port in 1-65535, else raise ValueError."""
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
        raise ValueError(f"{label} must be an integer between 1 and 65535, got {value!r}")


def _require_number(value: Any, label: str) -> None:
    """Require an int/float (not bool), else raise ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number, got {value!r}")


def _validate_follower(f: FollowerConfig) -> None:
    """Validate one follower's numeric fields; raise ValueError with context."""
    _require_port(f.port, f"follower '{f.name}' port")
    if isinstance(f.login, bool) or not isinstance(f.login, int) or f.login < 0:
        raise ValueError(
            f"Follower '{f.name}' login must be an integer >= 0, got {f.login!r}"
        )
    _require_number(f.lot_multiplier, f"follower '{f.name}' lot_multiplier")
    if f.lot_multiplier <= 0:
        raise ValueError(
            f"Follower '{f.name}' lot_multiplier must be > 0, got {f.lot_multiplier!r}"
        )
    _require_number(f.min_lot, f"follower '{f.name}' min_lot")
    _require_number(f.max_lot, f"follower '{f.name}' max_lot")
    if f.min_lot > f.max_lot:
        raise ValueError(
            f"Follower '{f.name}' min_lot ({f.min_lot}) must be <= max_lot ({f.max_lot}); "
            f"raise max_lot or lower min_lot"
        )


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
    skip_own_magic: bool = True  # skip re-broadcast of trades this follower itself placed
    symbol_mapping: dict[str, str] = field(default_factory=dict)
    skip_auto_trading: bool = False
    dry_run: bool = False
    terminal_data_path: str = ""  # MQL5/Files dir for file-based execution (Exness)
    max_daily_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    max_daily_trades: int = 0
    queue_path: str = "trade_queue.json"
    log_file: str = "logs/agent.log"
    log_level: str = "INFO"

    def validate(self) -> None:
        """Validate the agent config; raise ValueError with actionable messages."""
        # mt5_port 0 is allowed: it means "auto-detect the terminal IPC port".
        if isinstance(self.mt5_port, bool) or not isinstance(self.mt5_port, int) or not (self.mt5_port == 0 or 1 <= self.mt5_port <= 65535):
            raise ValueError(
                f"mt5_port must be an integer (0 = auto-detect, or 1-65535), got {self.mt5_port!r}"
            )
        if isinstance(self.mt5_login, bool) or not isinstance(self.mt5_login, int) or self.mt5_login < 0:
            raise ValueError(
                f"mt5_login must be an integer >= 0, got {self.mt5_login!r}"
            )
        _require_number(self.lot_multiplier, "lot_multiplier")
        if self.lot_multiplier <= 0:
            raise ValueError(f"lot_multiplier must be > 0, got {self.lot_multiplier!r}")
        _require_number(self.min_lot, "min_lot")
        _require_number(self.max_lot, "max_lot")
        if self.min_lot > self.max_lot:
            raise ValueError(
                f"min_lot ({self.min_lot}) must be <= max_lot ({self.max_lot}); "
                f"raise max_lot or lower min_lot"
            )
        if not self.mt5_login:
            raise ValueError("Agent config missing mt5_login")
        if not self.mt5_password:
            raise ValueError("Agent config missing mt5_password")
        if not self.mt5_server:
            raise ValueError("Agent config missing mt5_server")


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
        "skip_own_magic": f.skip_own_magic,
        "symbol_mapping": dict(f.symbol_mapping),
        "dry_run": f.dry_run,
        "terminal_data_path": f.terminal_data_path,
        "max_daily_loss": f.max_daily_loss,
        "max_drawdown_pct": f.max_drawdown_pct,
        "max_daily_trades": f.max_daily_trades,
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
            "ea_signals_file": cfg.master.ea_signals_file,
            "ea_watchdog": cfg.master.ea_watchdog,
            "ea_watchdog_attach_script": cfg.master.ea_watchdog_attach_script,
            "login": cfg.master.login,
            "password": cfg.master.password,
            "server": cfg.master.server,
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

# Skip events whose magic matches this follower's own (breaks same-account
# copy loops when master and follower share one account).
skip_own_magic: {str(f.skip_own_magic).lower()}

# Symbol mapping
symbol_mapping:
{chr(10).join(f'  "{k}": "{v}"' for k, v in f.symbol_mapping.items()) if f.symbol_mapping else "  # none"}

# Skip auto-trading enablement (GUI Ctrl+E automation).
# Set to true if you manually enable Algo button in MT5.
skip_auto_trading: {str(f.skip_auto_trading).lower()}

# Terminal data path (MQL5/Files dir) for file-based trade execution.
# Required when skip_auto_trading is true (Exness custom builds).
terminal_data_path: "{f.terminal_data_path}"

# Risk limits (0 = no limit)
max_daily_loss: {f.max_daily_loss}
max_drawdown_pct: {f.max_drawdown_pct}
max_daily_trades: {f.max_daily_trades}

# Local trade queue path (persisted on disk for outage resilience)
queue_path: "{f.queue_path}"

# Dry run: log trades without executing them (safety mode).
dry_run: {str(f.dry_run).lower()}

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
        cfg.master.ea_signals_file = m.get("ea_signals_file", cfg.master.ea_signals_file)
        cfg.master.ea_watchdog = bool(m.get("ea_watchdog", False))
        cfg.master.ea_watchdog_attach_script = m.get(
            "ea_watchdog_attach_script", cfg.master.ea_watchdog_attach_script
        )
        cfg.master.login = int(m.get("login", 0))
        cfg.master.password = m.get("password", "")
        cfg.master.server = m.get("server", "")

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
            skip_own_magic=bool(f_data.get("skip_own_magic", True)),
            symbol_mapping={k.upper(): v for k, v in (f_data.get("symbol_mapping") or {}).items()},
            max_daily_loss=float(f_data.get("max_daily_loss", 0.0)),
            max_drawdown_pct=float(f_data.get("max_drawdown_pct", 0.0)),
            max_daily_trades=int(f_data.get("max_daily_trades", 0)),
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
    cfg.validate()

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
        hub_url=raw.get("hub_url", "http://100.97.241.92:5000"),
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
        skip_own_magic=raw.get("skip_own_magic", True),
        symbol_mapping={k.upper(): v for k, v in (raw.get("symbol_mapping") or {}).items()},
        skip_auto_trading=raw.get("skip_auto_trading", False),
        dry_run=raw.get("dry_run", False),
        terminal_data_path=raw.get("terminal_data_path", ""),
        max_daily_loss=float(raw.get("max_daily_loss", 0.0)),
        max_drawdown_pct=float(raw.get("max_drawdown_pct", 0.0)),
        max_daily_trades=int(raw.get("max_daily_trades", 0)),
        queue_path=raw.get("queue_path", "trade_queue.json"),
        log_file=raw.get("log_file", "logs/agent.log"),
        log_level=raw.get("log_level", "INFO"),
    )

    cfg.validate()

    return cfg
