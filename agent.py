#!/usr/bin/env python3
"""Follower agent daemon — runs on each follower machine.

Connects to the hub (master machine) via WebSocket, registers itself with
identity info, receives trade events and config updates, executes trades
on the local MT5 terminal. Auto-reconnects on disconnect.

Acts as a background daemon: can be started at Windows login for persistent
operation without user interaction.

Usage:
    agent.exe [agent_config.yaml]
    python agent.py agent_config.yaml

    # Register for automatic startup at Windows login
    agent.exe --install
    agent.exe --remove
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path

# Fix Windows console encoding for unicode logging
if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.agent_client import AGENT_VERSION, AgentClient
from src.config import load_agent_config, FollowerConfig

logger = logging.getLogger("agent")


# ── Windows Startup helpers ─────────────────────────────────────

def _exe_path() -> str:
    """Return the path to the current executable or script."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def install_startup() -> None:
    """Register agent.exe to start automatically at Windows login.

    Uses schtasks to create a per-user scheduled task that runs on logon.
    Falls back to startup folder if schtasks fails.
    """
    exe = _exe_path()
    task_name = f"MT5CopyTrader-Agent-{Path(exe).stem}"

    # Use schtasks for reliable background launch (no console window)
    try:
        cmd = [
            "schtasks", "/Create", "/F",
            "/TN", task_name,
            "/TR", f'"{exe}"',
            "/SC", "ONLOGON",
            "/RL", "HIGHEST",
            "/DELAY", "0000:30",  # 30 second delay after logon
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print(f"✓ Startup task created: '{task_name}'")
            print(f"  Will start on next login (with 30s delay)")
            return
        print(f"✗ schtasks failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"✗ schtasks error: {e}")

    # Fallback: startup folder shortcut (shows console window)
    startup = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    if startup and os.path.isdir(startup):
        vbs_path = os.path.join(startup, f"{Path(exe).stem}.vbs")
        try:
            with open(vbs_path, "w") as f:
                f.write(f'CreateObject("Wscript.Shell").Run ""{exe}"", 0, False\n')
            print(f"✓ Startup shortcut created: {vbs_path}")
            return
        except Exception as e:
            print(f"✗ Startup folder error: {e}")

    print("✗ Could not register for startup")
    print("  Try: right-click agent.exe → Create shortcut → copy to Startup folder")


def remove_startup() -> None:
    """Remove the scheduled task created by install_startup()."""
    exe = _exe_path()
    task_name = f"MT5CopyTrader-Agent-{Path(exe).stem}"

    try:
        cmd = ["schtasks", "/Delete", "/F", "/TN", task_name]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print(f"✓ Startup task removed: '{task_name}'")
            return
    except Exception:
        pass

    # Also try startup folder
    startup = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    if startup:
        vbs_path = os.path.join(startup, f"{Path(exe).stem}.vbs")
        if os.path.exists(vbs_path):
            os.remove(vbs_path)
            print(f"✓ Removed: {vbs_path}")
            return

    print(f"✗ Could not find startup registration for {Path(exe).stem}")


# ── Config bootstrap ────────────────────────────────────────────

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


# ── Logging ──────────────────────────────────────────────────────

def setup_logging(log_file: str = "logs/agent.log", level: str = "INFO") -> None:
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


# ── Main ─────────────────────────────────────────────────────────

def main() -> None:
    # Portable: chdir to exe directory when frozen
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    # Handle startup registration commands
    if "--install" in sys.argv:
        install_startup()
        return
    if "--remove" in sys.argv:
        remove_startup()
        return

    config_path = "agent_config.yaml"
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            config_path = arg
            break

    # Auto-create default agent config on first run
    _ensure_config(config_path)

    cfg = load_agent_config(config_path)
    setup_logging(cfg.log_file, cfg.log_level)

    agent_id = f"{cfg.name}-{platform.node()}-{os.path.basename(cfg.mt5_path)}"

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
        agent_id=agent_id,
    )

    logger.info("=" * 60)
    logger.info("  MT5 Copy Trade Agent v%s", AGENT_VERSION)
    logger.info("  Name:     %s", cfg.name)
    logger.info("  Hub:      %s", cfg.hub_url)
    logger.info("  MT5:      %s @%s:%d", cfg.mt5_login, cfg.mt5_server, cfg.mt5_port)
    logger.info("  Hostname: %s", platform.node())
    logger.info("=" * 60)

    # Signal handling for graceful shutdown
    def shutdown_handler(signum=None, frame=None) -> None:
        logger.info("Shutdown signal received, stopping...")
        client.stop()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        shutdown_handler()
    finally:
        logger.info("Agent stopped")
        logging.shutdown()


if __name__ == "__main__":
    main()
