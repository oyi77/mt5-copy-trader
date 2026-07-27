#!/usr/bin/env python3
"""Copy Trade Engine — Master Process.

Starts:
  1. Bridge thread (polls master MT5, detects changes)
  2. aiohttp server (dashboard REST + SSE + agent WebSocket hub)
  3. Everything runs until Ctrl+C

Usage:
    run.exe [config.yaml]
    python run.py [config.yaml]
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import threading
from pathlib import Path

# Fix Windows console encoding for unicode logging
if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


from aiohttp import web

from src.bridge import CopyTradeBridge
from src.config import load_config, save_config
from src.server import create_app
from src.state import SharedState


def _bundle_path() -> str:
    """Return the PyInstaller bundle directory, or empty string if not frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return ""


def _ensure_config(path: str) -> None:
    """Create default config.yaml from bundled default if it doesn't exist."""
    if not path or path.startswith("-"):
        return
    if os.path.exists(path):
        return
    bundle = _bundle_path()
    if bundle and os.path.exists(os.path.join(bundle, "config.yaml")):
        import shutil
        shutil.copy2(os.path.join(bundle, "config.yaml"), path)
        print(f"Created default config: {path}")
    else:
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            "Copy config.yaml from the repo, or run with a path argument."
        )


def setup_logging(log_dir: str, log_file: str, level_str: str) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-5s | %(name)-16s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=50 * 1024 * 1024,
        backupCount=3,
    )
    fh.setLevel(getattr(logging, level_str.upper(), logging.INFO))
    fh.setFormatter(formatter)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, level_str.upper(), logging.INFO))
    ch.setFormatter(formatter)
    root.addHandler(ch)

    return logging.getLogger("main")


def main() -> None:
    # Ensure CWD is the directory where the exe lives (for portable use)
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    # Auto-create default config if missing (useful for first run / frozen builds)
    if not config_path.startswith("-"):
        _ensure_config(config_path)

    cfg = load_config(config_path)

    log_dir = os.path.dirname(cfg.logging.file) if cfg.logging.file else ""
    logger = setup_logging(log_dir, cfg.logging.file, cfg.logging.level)

    # ── Shared state + event queue ────────────────────────────
    state = SharedState()
    event_queue = asyncio.Queue(maxsize=200)

    # ── Create asyncio loop (runs in background thread) ──────
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ── Create bridge ────────────────────────────────────────
    bridge = CopyTradeBridge(cfg, state, event_queue, loop)

    # ── Create aiohttp app ────────────────────────────────────
    app = create_app(state, event_queue, cfg, config_path, bridge)
    runner = web.AppRunner(app)

    async def start_server():
        try:
            await runner.setup()
            site = web.TCPSite(runner, cfg.host, cfg.port)
            await site.start()
            logger.info("Dashboard + hub: http://%s:%d", cfg.host, cfg.port)
        except Exception as e:
            logger.error("Failed to start dashboard server: %s", e, exc_info=True)

    # ── Start bridge thread ──────────────────────────────────
    bridge_thread = threading.Thread(target=bridge.run, daemon=True, name="bridge")
    bridge_thread.start()
    logger.info("Bridge thread started")

    # ── Start asyncio server ─────────────────────────────────
    asyncio.run_coroutine_threadsafe(start_server(), loop)

    def sigint_handler(signum, frame):
        logger.info("Shutting down...")
        bridge.stop()
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGTERM, sigint_handler)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
