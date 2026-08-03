# AGENTS.md

## Project

MT5 Copy Trader replicates trades from a master MetaTrader 5 terminal to multiple follower terminals — same PC or remote machines. The master process (`run.py`) polls the master MT5 terminal for position/order changes and broadcasts events over a WebSocket hub; follower agents (`agent.py`) receive them and execute on their own MT5 terminals. A REST + SSE dashboard exposes status and configuration. Remote connectivity assumes a trusted network (LAN or Tailscale).

## Layout

| Path | Purpose |
|------|---------|
| `run.py` | Master entry point: bridge thread (MT5 polling + change detection) + aiohttp dashboard/hub server |
| `agent.py` | Follower agent entry point: WS client, local MT5 execution, Windows startup registration (`--install` / `--remove` / `--install-mt5`) |
| `src/` | Core library: `bridge` (poll + broadcast loop), `server` (REST/SSE dashboard + WS agent hub), `config` (dataclasses + YAML load/save), `state` (thread-safe shared state + SQLite EventStore), `master` (MT5 polling), `master_ea` (EA signal-file tailer — no MetaTrader5 import), `ea_watchdog` (EA-mode terminal/EA auto-recovery), `follower` (trade execution, risk limits, file relay), `terminal_ui` (Windows window automation: Algo-button toggle, dialog dismissal), `config_push` (hub config validation + schema conversion for agents), `agent_client` (WS client daemon), `models` (Position/PendingOrder/TradeEvent) |
| `TradeSender.mq5` | Master-side EA signal emitter — polls the account and appends pipe-delimited OPEN/CLOSE/MODIFY/PLACE/DELETE/MODIFY_ORDER lines to `MQL5\Files\master_signals.txt` (EA-only master mode, see README). String fields (symbol/comment/account name) are `\|`-escaped on write |
| `TradeReceiver.mq5` | Follower-side EA — executes OPEN_BUY/OPEN_SELL/CLOSE/MODIFY/PLACE_ORDER/MODIFY_ORDER/DELETE_ORDER/CLOSE_ALL commands dropped into `MQL5\Files\pending.txt` by the file-relay follower path, writes the result to `result.txt`. Uses paths relative to `MQL5\Files\` (MQL5 rejects absolute paths) |
| `attach_ea.ps1` | UI-automation helper that re-attaches an EA to a chart (Navigator double-click — MT5's Insert menu does not select by typed name); used manually and as `ea_watchdog_attach_script` |
| `install_ea.py` | One-command EA installer: deploys the prebuilt `.ex5` (or compiles via `--compile`) into a terminal data dir and attaches it to a `chartNN.chr` profile so the terminal loads the EA at startup with no UI automation. `--role master` = TradeSender, `--role follower` = TradeReceiver; `--data-dir` or `--config` picks the terminal |
| `TradeSender.ex5` / `TradeReceiver.ex5` | Prebuilt binaries (deterministic builds of the adjacent sources, live-verified) deployed by `install_ea.py` when MetaEditor is unavailable |
| `static/` | Dashboard frontend (`index.html`, `app.js`) |
| `tests/` | Unit tests — run with `python -m unittest discover -s tests` |
| `scratch/` | Recovered debug artifacts from past sessions — not product code; safe to ignore |
| `config.yaml` | Master + same-PC follower configuration |
| `agent_config.yaml` | Follower agent configuration (hub URL, MT5 credentials) |
| `build.py` / `package.py` | PyInstaller exe build / distribution zip |
| `audit_report.py` | Read-only weekly audit: uptime %, signal counts, top errors, signal gaps (see README) |

## Conventions

- Every module starts with a docstring describing its role; public classes and functions get docstrings too.
- Use type hints (`from __future__ import annotations`).
- Each module defines a module-level `logger = logging.getLogger(__name__)`; no `print`-based logging inside `src/`.
- The REST routes, SSE stream, WebSocket message types, and JSON field names are a contract shared by the server, dashboard, and agents — keep them stable and only add to them (never rename or remove).
- Never execute code that imports `MetaTrader5` in tests or CI — mock it via `sys.modules`.

## Run / Test / Build

- Master: `python run.py [config.yaml]`
- Agent: `python agent.py agent_config.yaml` (flags: `--install`, `--remove`, `--install-mt5`, `--hub <url>`)
- Tests: `python -m unittest discover -s tests`
- Syntax check after edits: `python -m py_compile <changed files>`
- Build: `python build.py` (produces `dist/run.exe` + `dist/agent.exe`), `python package.py` (produces `dist/copy-trade-engine.zip`)

## Do not touch

Runtime data and build artifacts — never edit, delete, or commit:

- `logs/`
- `event_store.db`, `event_store.db-shm`, `event_store.db-wal` (and any `event_store.db*`)
- `mt5_exness/`
- `dist/`, `build/`

Root-level `_*.py` / `_*.ps1` / `_*.bin` files are leftover debug/recovery scripts from past sessions (see `scratch/`); treat them as artifacts unless explicitly asked to work on one.
