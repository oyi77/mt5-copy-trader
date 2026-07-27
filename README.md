# MT5 Copy Trader

Replicate live trades from a master MetaTrader 5 terminal to multiple follower terminals — same PC or across machines via WebSocket (Tailscale recommended).

## Architecture

```
┌──────────────┐    asyncio.Queue    ┌──────────┐    WebSocket     ┌──────────────┐
│  Master MT5  │ ──────────────────▶ │   Hub    │ ───────────────▶ │ Follower #1  │
│  (trade here)│   poll 300ms        │ (server) │    port 5000     │  (remote PC) │
└──────────────┘                     └──────────┘                  ├──────────────┤
                                     │         │                   │ Follower #2  │
                                     ▼         │                   └──────────────┘
                              ┌──────────┐     │
                              │ Dashboard │     │   Or same-PC followers
                              │ REST+SSE  │     │   (MT5 terminal on diff port)
                              │ port 5000 │     └──────────────────────────────▶
                              └──────────┘
```

## Quick Start

### Master machine
```bash
# Install
pip install -r requirements.txt

# Run
python run.py

# Dashboard: http://localhost:5000
```

### Follower (remote PC)
```bash
pip install -r requirements.txt
python agent.py
```

### Standalone executables (no Python needed)
```bash
python build.py
# Produces: dist/run.exe + dist/agent.exe
python package.py
# Produces: dist/copy-trade-engine.zip (~62 MB)
```

## Features

- **Real-time copy**: polls master MT5 every 300ms, detects open/close/modify
- **WebSocket agents**: remote followers auto-connect with exponential backoff
- **Dashboard**: portfolio overview, equity chart, per-account positions, activity log
- **Config UI**: manage master, server, and followers from browser
- **PyInstaller build**: standalone .exe for Windows, no Python required
- **SSE live updates**: dashboard values update in-place every ~2s

## Requirements

- Python 3.12+
- MetaTrader 5 terminal (master + each follower)
- Tailscale for remote agents (or port forwarding)

## License

MIT
