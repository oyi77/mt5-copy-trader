"""Hub-pushed configuration for follower agents: validation + conversion.

The agent's WebSocket client receives two config message types from the hub:
- ``config_update`` — partial overrides applied onto the running FollowerConfig
- ``config_deploy`` — a full agent payload that transitions the agent from
  unconfigured to configured (persisted as agent_config.yaml, MT5 installed,
  executor built)

All validation and schema conversion lives here as pure functions so the
agent client stays a thin transport/dispatcher and the rules are unit-testable
without MT5 or a network connection.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from src.config import FollowerConfig

logger = logging.getLogger(__name__)


def parse_bool(value, default: bool = False) -> bool:
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


def parse_int(value, name: str, minv: Optional[int] = None, maxv: Optional[int] = None) -> int:
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


def parse_float(value, name: str, minv: Optional[float] = None, maxv: Optional[float] = None) -> float:
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


def resolve_queue_path(config: dict, config_save_path: str) -> str:
    """Resolve the queue path to an absolute location next to the saved config
    (survives scheduled-task/SSH cwd differences)."""
    queue_path = config.get("queue_path", "trade_queue.json")
    if not os.path.isabs(queue_path):
        queue_path = os.path.join(os.path.dirname(config_save_path), queue_path)
    return queue_path


@dataclass
class DeployValues:
    """Validated, normalized values extracted from a deploy payload."""

    port: int
    login: int
    lot_multiplier: float
    max_lot: float
    min_lot: float
    max_positions: int
    deviation: int
    magic: int
    max_daily_loss: float
    max_drawdown_pct: float
    max_daily_trades: int
    symbol_mapping: dict
    queue_path: str


def parse_deploy_config(config: dict, config_save_path: str) -> DeployValues:
    """Validate + normalize a deploy payload; raises ValueError on bad input."""
    port = parse_int(config.get("port", 0), "port", minv=0, maxv=65535)
    login = parse_int(config.get("login", 0), "login", minv=0)
    lot_multiplier = parse_float(config.get("lot_multiplier", 1.0), "lot_multiplier", minv=0.0)
    max_lot = parse_float(config.get("max_lot", 10.0), "max_lot", minv=0.0)
    min_lot = parse_float(config.get("min_lot", 0.01), "min_lot", minv=0.0)
    max_positions = parse_int(config.get("max_positions", 10), "max_positions", minv=0)
    deviation = parse_int(config.get("deviation", 50), "deviation", minv=0)
    magic = parse_int(config.get("magic", 951001), "magic", minv=0)
    max_daily_loss = parse_float(config.get("max_daily_loss", 0.0), "max_daily_loss", minv=0.0)
    max_drawdown_pct = parse_float(config.get("max_drawdown_pct", 0.0), "max_drawdown_pct", minv=0.0)
    max_daily_trades = parse_int(config.get("max_daily_trades", 0), "max_daily_trades", minv=0)

    raw_mapping = config.get("symbol_mapping", {})
    if not isinstance(raw_mapping, dict):
        logger.warning("Deploy config: symbol_mapping ignored (not an object)")
        raw_mapping = {}
    symbol_mapping = {k.upper(): v for k, v in raw_mapping.items()}

    return DeployValues(
        port=port,
        login=login,
        lot_multiplier=lot_multiplier,
        max_lot=max_lot,
        min_lot=min_lot,
        max_positions=max_positions,
        deviation=deviation,
        magic=magic,
        max_daily_loss=max_daily_loss,
        max_drawdown_pct=max_drawdown_pct,
        max_daily_trades=max_daily_trades,
        symbol_mapping=symbol_mapping,
        queue_path=resolve_queue_path(config, config_save_path),
    )


def build_agent_config_dict(
    config: dict, values: DeployValues, *,
    agent_name: str, hub_url: str, data_dir: str,
) -> dict:
    """Convert a deploy payload into the AgentConfig YAML schema.

    The dashboard payload uses FollowerConfig keys (login/path/port/...);
    this maps them to the agent_config.yaml schema (mt5_login/mt5_path/...)
    so that load_agent_config() succeeds on restart. hub_url is taken from
    the connection the agent is actually using (e.g. the reverse-tunnel URL),
    and log_file is forced into the user-writable data dir (filtered
    scheduled-task tokens can't write relative paths like C:\\logs\\agent.log).
    """
    return {
        "name": config.get("name", agent_name),
        "hub_url": hub_url,
        "mt5_path": config.get("path", "C:/Program Files/MetaTrader 5/terminal64.exe"),
        "mt5_port": values.port,
        "mt5_login": values.login,
        "mt5_password": config.get("password", ""),
        "mt5_server": config.get("server", ""),
        "lot_multiplier": values.lot_multiplier,
        "max_lot": values.max_lot,
        "min_lot": values.min_lot,
        "max_positions": values.max_positions,
        "deviation": values.deviation,
        "magic": values.magic,
        "skip_own_magic": parse_bool(config.get("skip_own_magic", True), True),
        "symbol_mapping": values.symbol_mapping,
        "skip_auto_trading": parse_bool(config.get("skip_auto_trading", True), True),
        "dry_run": parse_bool(config.get("dry_run", False), False),
        "terminal_data_path": config.get("terminal_data_path", ""),
        "max_daily_loss": values.max_daily_loss,
        "max_drawdown_pct": values.max_drawdown_pct,
        "max_daily_trades": values.max_daily_trades,
        "queue_path": values.queue_path,
        "log_file": os.path.join(data_dir, "logs", "agent.log"),
        "log_level": "INFO",
    }


def build_follower_config(config: dict, values: DeployValues, *, agent_name: str) -> FollowerConfig:
    """Build the live FollowerConfig for a deploy payload.

    Note: max_lot defaults to 1.0 here (not 10.0 as in the YAML schema) —
    kept identical to the historical agent behaviour.
    """
    return FollowerConfig(
        name=config.get("name", agent_name),
        path=config.get("path", "C:\\Program Files\\MetaTrader 5\\terminal64.exe"),
        port=values.port,
        login=values.login,
        password=config.get("password", ""),
        server=config.get("server", ""),
        lot_multiplier=values.lot_multiplier,
        max_lot=parse_float(config.get("max_lot", 1.0), "max_lot", minv=0.0),
        min_lot=values.min_lot,
        max_positions=values.max_positions,
        deviation=values.deviation,
        magic=values.magic,
        skip_own_magic=parse_bool(config.get("skip_own_magic", True), True),
        symbol_mapping=values.symbol_mapping,
        skip_auto_trading=parse_bool(config.get("skip_auto_trading", True), True),
        terminal_data_path=config.get("terminal_data_path", ""),
        max_daily_loss=values.max_daily_loss,
        max_drawdown_pct=values.max_drawdown_pct,
        max_daily_trades=values.max_daily_trades,
        queue_path=values.queue_path,
    )


@dataclass
class ApplyResult:
    """Outcome of applying a partial config_update payload."""

    changed: list
    errors: list
    # New resolved queue path when a valid queue_path override was applied.
    queue_path: Optional[str] = None


def apply_updates(cfg: FollowerConfig, config: dict) -> ApplyResult:
    """Apply partial overrides from a config_update message onto the running
    FollowerConfig. Invalid values are rejected per-field (collected in
    errors) instead of raising and killing the WS connection."""
    if not config:
        return ApplyResult(changed=[], errors=[])

    errors: list = []
    changed: list = []

    def _field_float(key: str, minv: Optional[float] = None) -> Optional[float]:
        if key not in config:
            return None
        try:
            return parse_float(config[key], key, minv=minv)
        except ValueError as e:
            errors.append(str(e))
            return None

    def _field_int(key: str, minv: Optional[int] = None) -> Optional[int]:
        if key not in config:
            return None
        try:
            return parse_int(config[key], key, minv=minv)
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

    queue_path: Optional[str] = None
    if "queue_path" in config:
        old = cfg.queue_path
        new_qp = config["queue_path"]
        if not isinstance(new_qp, str) or not new_qp.strip():
            errors.append(f"queue_path: invalid value {new_qp!r}")
        else:
            cfg.queue_path = new_qp
            queue_path = new_qp
            changed.append(f"queue_path: {old} → {cfg.queue_path}")

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
    return ApplyResult(changed=changed, errors=errors, queue_path=queue_path)
