#!/usr/bin/env python3
"""Follower agent — runs on each follower machine.

Connects to the hub (master machine) via WebSocket, receives trade events,
and executes them on the local MT5 terminal.

Usage:
    agent.exe [agent_config.yaml]
    python agent.py agent_config.yaml
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys

# Fix Windows console encoding for unicode logging
if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


from src.agent_client import AgentClient
from src.config import load_agent_config, FollowerConfig


def _bundle_path() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return ""


def _ensure_config(path: str) -> None:
    """Create default agent_config.yaml from bundled default if missing."""
    if os.path.exists(path):
        return
    bundle = _bundle_path()
    if bundle and os.path.exists(os.path.join(bundle, "agent_config.yaml")):
        import shutil
        shutil.copy2(os.path.join(bundle, "agent_config.yaml"), path)
        print(f"Created default agent config: {path}")
    else:
        raise FileNotFoundError(
            f"Agent config not found: {path}\n"
            "Copy agent_config.yaml from the repo, or run with a path argument."
        )


def setup_logging(log_file: str = "logs/agent.log", level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-5s | %(name)-16s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=50 * 1024 * 1024, backupCount=3,
    )
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh.setFormatter(formatter)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, level.upper(), logging.INFO))
    ch.setFormatter(formatter)
    root.addHandler(ch)

    return logging.getLogger("agent")


def main() -> None:
    # Portable: chdir to exe directory when frozen
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    config_path = sys.argv[1] if len(sys.argv) > 1 else "agent_config.yaml"

    # Auto-create default agent config on first run
    if not config_path.startswith("-"):
        _ensure_config(config_path)

    cfg = load_agent_config(config_path)
    setup_logging(cfg.log_file, cfg.log_level)

    follower_cfg = FollowerConfig(
        name=cfg.name,
        path=cfg.mt5_path,
        port=cfg.mt5_port,
        login=cfg.mt5_login,
        password=cfg.mt5_password,
        server=cfg.mt5_server,
        lot_multiplier=cfg.lot_multiplier,
        max_lot=cfg.max_lot,
        min_lot=cfg.min_lot,
        max_positions=cfg.max_positions,
        deviation=cfg.deviation,
        magic=cfg.magic,
        symbol_mapping={k.upper(): v for k, v in cfg.symbol_mapping.items()},
    )

    client = AgentClient(
        hub_url=cfg.hub_url,
        follower_cfg=follower_cfg,
        agent_name=cfg.name,
    )

    def sigint_handler(signum, frame):
        logging.getLogger("agent").info("Shutting down...")
        client.stop()

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
