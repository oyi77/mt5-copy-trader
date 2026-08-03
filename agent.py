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
import tempfile
import urllib.request
from pathlib import Path

# Fix Windows console encoding for unicode logging
if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.agent_client import AGENT_VERSION, AgentClient
from src.config import load_agent_config, FollowerConfig

logger = logging.getLogger("agent")


def get_data_dir() -> str:
    """Return a user-writable directory for config/logs/queue files.

    Scheduled-task and SSH-launched processes run with a filtered token that
    cannot write to the C: root (no Authenticated Users group), so we keep all
    agent state under %LOCALAPPDATA%\\MT5CopyAgent (or the source dir when
    running from source).
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        data_dir = os.path.join(base, "MT5CopyAgent")
    else:
        data_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# ── MT5 Installation ──────────────────────────────────────────────

MT5_INSTALL_URL = "https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe"
MT5_DEFAULT_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"


def install_standard_mt5() -> bool:
    """Download and silently install standard MetaTrader 5.

    Checks if MT5 is already installed at the default path. Skips
    download if present unless --force was passed.

    Returns True on success, False on failure.
    """
    force = "--force" in sys.argv

    # Check if already installed
    if os.path.exists(MT5_DEFAULT_PATH) and not force:
        print("✓ MetaTrader 5 already installed at:")
        print(f"  {MT5_DEFAULT_PATH}")
        print("  Use --force to reinstall.")
        return True

    # Download the installer
    print("Downloading MetaTrader 5 installer...")
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".exe", prefix="mt5setup_", delete=False
        ) as tmp:
            installer_path = tmp.name
            urllib.request.urlretrieve(MT5_INSTALL_URL, installer_path)
        print(f"  Downloaded to {installer_path}")
    except Exception as e:
        print(f"✗ Download failed: {e}")
        return False

    # Run installer silently
    print("Running installer with /auto...")
    try:
        result = subprocess.run(
            [installer_path, "/auto"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print("✓ MetaTrader 5 installed successfully")
            installed = os.path.exists(MT5_DEFAULT_PATH)
            if installed:
                print(f"  Located at: {MT5_DEFAULT_PATH}")
            return True
        else:
            print(f"✗ Installer failed (code {result.returncode})")
            if result.stderr.strip():
                print(f"  stderr: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print("✗ Installer timed out after 5 minutes")
        return False
    except Exception as e:
        print(f"✗ Installer error: {e}")
        return False
    finally:
        # Clean up installer
        try:
            os.unlink(installer_path)
        except Exception:
            pass


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
    if "--install-mt5" in sys.argv:
        success = install_standard_mt5()
        sys.exit(0 if success else 1)

    # Parse args: --hub <url> [config_path]
    config_path = "agent_config.yaml"
    hub_url = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--hub" and i + 1 < len(args):
            hub_url = args[i + 1]
            i += 2
            continue
        if not args[i].startswith("-"):
            config_path = args[i]
        i += 1

    # User data dir for config/logs/queue (scheduled-task safe location)
    data_dir = get_data_dir()
    data_config_path = os.path.join(data_dir, os.path.basename(config_path))

    # Auto-extract bundled config for frozen builds (if no config yet)
    if not os.path.exists(config_path) and not hub_url:
        try:
            if getattr(sys, "frozen", False):
                _ensure_config(data_config_path)
                config_path = data_config_path
            else:
                _ensure_config(config_path)
        except FileNotFoundError as e:
            # Builds no longer bundle agent_config.yaml, so this is the normal
            # first-run path: print the guidance and exit cleanly instead of
            # crashing with a traceback.
            print(e)
            sys.exit(1)

    # Also look for config in the user data dir (scheduled-task safe location)
    if not os.path.exists(config_path) and os.path.exists(data_config_path):
        config_path = data_config_path

    # Try to load config; if missing and --hub given, enter unconfigured mode
    try:
        cfg = load_agent_config(config_path)
        configured = True
    except FileNotFoundError:
        if hub_url:
            configured = False
            cfg = None
        else:
            print(
                "No config file found. Use --hub <url> to connect unconfigured, "
                "or provide agent_config.yaml"
            )
            sys.exit(1)

    if configured:
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
            skip_own_magic=cfg.skip_own_magic,
            symbol_mapping={k.upper(): v for k, v in cfg.symbol_mapping.items()},
            skip_auto_trading=cfg.skip_auto_trading,
            terminal_data_path=cfg.terminal_data_path,
        )

        client = AgentClient(
            # CLI --hub takes precedence (tunnel/override targeting);
            # otherwise fall back to the persisted config value.
            hub_url=hub_url or cfg.hub_url,
            follower_cfg=follower_cfg,
            agent_name=cfg.name,
            agent_id=agent_id,
            event_store_path=os.path.join(data_dir, "trade_events.json"),
            config_save_path=data_config_path,
        )

        logger.info("=" * 60)
        logger.info("  MT5 Copy Trade Agent v%s", AGENT_VERSION)
        logger.info("  Name:     %s", cfg.name)
        logger.info("  Hub:      %s", cfg.hub_url)
        logger.info("  MT5:      %s @%s:%d", cfg.mt5_login, cfg.mt5_server, cfg.mt5_port)
        logger.info("  Hostname: %s", platform.node())
        logger.info("=" * 60)
    else:
        # Unconfigured mode: no config file, --hub provided
        agent_name = platform.node()
        agent_id = f"{agent_name}-pending"

        # Keep config/logs/queue in the user data dir so the agent works
        # regardless of working directory or token filtering (scheduled tasks,
        # SSH, double-click). Frozen builds store under %LOCALAPPDATA%\MT5CopyAgent.
        save_path = data_config_path
        log_path = os.path.join(data_dir, "logs", "agent.log")
        client = AgentClient(
            hub_url=hub_url,
            follower_cfg=None,
            agent_name=agent_name,
            agent_id=agent_id,
            event_store_path=os.path.join(data_dir, "trade_events.json"),
            config_save_path=save_path,
        )

        print("=" * 60)
        print("  MT5 Copy Trade Agent (unconfigured)")
        print("  Hub:      %s" % hub_url)
        print("  Hostname: %s" % platform.node())
        print("  Waiting for config push from dashboard...")
        print("=" * 60)

        # Use basic logging in unconfigured mode
        setup_logging(log_path, "INFO")

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
