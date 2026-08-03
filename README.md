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

The master process (`run.py`) runs a bridge thread that polls the master MT5 terminal (default 300 ms, configurable via `poll_interval_ms`), detects position/order changes, and pushes events to the hub over an `asyncio.Queue`. The hub broadcasts them to connected follower agents over WebSocket and serves the dashboard over REST + SSE. Every event is persisted to a SQLite EventStore (`event_store.db`) before broadcast; agents resume from their last processed `seq_id` on reconnect and the hub replays any missed events.

## Quick Start

### Master machine
```bash
# Install
pip install -r requirements.txt

# Run (optional config path, defaults to config.yaml)
python run.py [config.yaml]

# Dashboard: http://localhost:5000
```

### Follower (remote PC)
```bash
pip install -r requirements.txt
python agent.py agent_config.yaml

# Optional agent flags:
#   --install       register the agent to auto-start at Windows login
#   --remove        remove that startup registration
#   --install-mt5   download and silently install standard MetaTrader 5
#   --hub <url>     connect unconfigured; wait for a config push from the dashboard
```

### Standalone executables (no Python needed)
```bash
python build.py
# Produces: dist/run.exe + dist/agent.exe
python package.py
# Produces: dist/copy-trade-engine.zip
```

## EA-only master mode (no MetaTrader5 package)

Some Exness builds reject the MT5 IPC handshake (error `-6`), and the
`MetaTrader5` Python package must exactly match the terminal build. If you run
the master without it, attach the included `TradeSender.mq5` EA to any chart of
the master terminal and let the bridge read its signal file instead:

**Quick install** — `install_ea.py` copies (or optionally compiles) the EA
into `<terminal data>\MQL5\Experts\` and attaches it to a chart profile
(`chartNN.chr`) so the terminal loads it at startup with no UI automation:

```bash
# Master side (TradeSender -> master_signals.txt)
python install_ea.py --role master --data-dir "C:/.../Terminal/<hash>" --chart 1
# Follower side (TradeReceiver -> pending.txt/result.txt)
python install_ea.py --role follower --config agent_config.yaml --chart 1 --magic 200001
```

`--data-dir` is the folder containing `MQL5\` (for the master it is the parent
of the signal file). By default the prebuilt `.ex5` kept beside the script is
deployed — no compiler needed; pass `--compile` to rebuild from source with
MetaEditor (best-effort: MetaEditor's headless `/compile` can silently no-op on
some machines, in which case the installer falls back to the bundled build).
After installing, restart the terminal once so the chart profile is loaded,
and keep the EA's chart as the active tab (see the timer note below).

The manual steps (for those who prefer not to use the installer):

1. Copy `TradeSender.mq5` (or the prebuilt `TradeSender.ex5`) into
   `<terminal data>\MQL5\Experts\` and compile it in MetaEditor if you use the
   source.
2. Attach TradeSender to any chart of the master terminal — any symbol works,
   it diffs the whole account, not just the chart. It appends one pipe-delimited
   line per detected change to `MQL5\Files\master_signals.txt` plus a
   STATUS/HEARTBEAT pair every 10 s.
3. In `config.yaml`, point the bridge at the signal file:
   ```yaml
   master:
     path: C:/Users/me/mt5/terminal64.exe   # unused for polling in EA mode
     port: 22346
     # Absolute path to MQL5\Files\master_signals.txt in the master data folder
     ea_signals_file: C:/Users/me/AppData/Roaming/MetaQuotes/Terminal/<hash>/MQL5/Files/master_signals.txt
   ```
4. Start the bridge exactly as usual: `python run.py config.yaml`.

How it works:

- The EA polls every 500 ms, diffs positions and pending orders against its
  previous snapshot, and appends `OPEN` / `CLOSE` / `MODIFY` / `PLACE` /
  `DELETE` / `MODIFY_ORDER` lines (market orders that flash through the order
  pool during execution are ignored). Positions already open when the EA
  attaches are baselined and never relayed — the same rule as the IPC path.
- `src/master_ea.py` tails the file (no `MetaTrader5` import), persists the
  last processed `SEQ` to a sidecar (`master_signals.txt.state.json`) so bridge
  restarts resume where they left off, and feeds the same event pipeline as the
  IPC path: hub broadcast, SQLite EventStore, follower execution, dashboard.
- The dashboard's `master_connected` flag turns false when the signal file
  disappears or no STATUS/HEARTBEAT arrives for 30 s — i.e. the EA is not
  running.

Notes:

- MQL5's `FileOpen` accepts only paths relative to `MQL5\Files\`, so the EA
  always writes to that folder no matter where the terminal is installed.
- A hard kill of the terminal does not save the chart profile, so the EA does
  not auto-restart — re-attach TradeSender after such a restart (the included
  `attach_ea.ps1` does this, or enable `ea_watchdog` below for automatic
  recovery).
- The timer only fires while the EA's chart is the **active/visible tab** —
  MT5 pauses `OnTimer` for EAs on background chart tabs (verified live: an EA
  attached to a non-active tab never consumed its command file, while the same
  EA on the active tab executed within the poll interval). Keep the chart the
  EA is attached to in the foreground.
- Attaching without UI: edit the chart profile `chart*.chr` in
  `<terminal data>\MQL5\Profiles\Charts\DEFAULT\` (UTF-16LE, `\r\n` lines) and
  add an `<expert>` block pointing at the `.ex5`; the terminal loads every
  `chart*.chr` in that folder at startup, so a restart attaches the EA
  deterministically with no mouse clicks. (`attach_ea.ps1` clicks the
  Navigator tree, which fails when the terminal window is minimized.)
- Symbol, comment, and account-name fields may contain `|` or `\`: the EA
  escapes them (`\` → `\\`, `|` → `\|`) when writing and `src/master_ea.py`
  unescapes on read, so no field content can break the line format.
- Pending-order events (`PLACE` / `MODIFY_ORDER` / `DELETE`) are relayed to the
  hub for agents, and the same-PC file-relay follower executes them too —
  TradeReceiver understands `PLACE_ORDER`, `MODIFY_ORDER` and `DELETE_ORDER`
  commands in addition to market open/close/modify.
- The follower side has its own independent file-relay mode
  (`terminal_data_path` + `TradeReceiver.mq5`); the two are separate. Install
  the receiver with `python install_ea.py --role follower --config
  agent_config.yaml --chart 1` — it deploys the EA and prints the
  `terminal_data_path` to set on the agent (the terminal's `MQL5\Files`
  folder).

### EA watchdog (optional auto-recovery)

When the master runs in EA mode, the bridge can detect a dead terminal/EA and
bring it back automatically:

```yaml
master:
  ea_signals_file: .../master_signals.txt
  # Gracefully close + relaunch the terminal and, if the EA still does not
  # emit heartbeats, run the attach script to re-attach TradeSender.
  ea_watchdog: true
  ea_watchdog_attach_script: C:/path/to/attach_ea.ps1
  # Used only by the watchdog's terminal relaunch (MT5 /login switch):
  login: 433903489
  password: "your-demo-password"
  server: Exness-MT5Trial7
```

When the signal file goes stale for 30 s, the watchdog closes the master
terminal, relaunches it logged into the same account, waits for heartbeats, and
as a last resort runs `ea_watchdog_attach_script` (the included
`attach_ea.ps1`) to re-attach TradeSender to a chart. A plain relaunch is
usually enough: the chart profile (`Profiles/Charts/Default/*.chr`) stores the
attached EA, so TradeSender auto-loads ~2 s after the terminal starts and the
heartbeat resumes without any UI automation (verified live on Exness MT5
build 6090). Events emitted by the EA while the terminal was down are
collected and re-broadcast once it is back (at-least-once delivery). Default
is off (`ea_watchdog: false`).

### Weekly audit report
```bash
python audit_report.py [--days 7] [--db event_store.db]
                       [--log logs/copy_trade.log] [--gap-minutes 60]
                       [--out report.md]
# Reads the event store + engine log (read-only, safe while live) and prints
# uptime %, signal counts by action/symbol/day, top error messages, and
# silence gaps between signals that may indicate missed trades.
```

## Features

- **Real-time copy**: polls the master MT5 terminal (`poll_interval_ms`, default 300 ms) and detects open / close / modify, plus pending-order place / modify / delete; positions that existed before startup are snapshotted and not auto-copied
- **EA-only master (Exness)**: set `master.ea_signals_file` to tail `TradeSender.mq5`'s signal file — no `MetaTrader5` package or IPC handshake needed on the master side
- **WebSocket agents**: remote followers connect to `/ws/agent`, auto-reconnect with exponential backoff, and report heartbeat/status to the hub
- **At-least-once delivery**: every event is written to the SQLite EventStore before broadcast; on reconnect each agent sends its `last_seq_id` and the hub replays missed events
- **Same-PC followers**: followers defined in `config.yaml` run on their own MT5 installations/ports and can be activated/deactivated from the dashboard, with login verification and automatic Algo-button (auto-trading) enablement
- **File-relay mode (Exness)**: when `terminal_data_path` is set, the agent writes trade commands to the terminal's MQL5/Files directory for a companion EA (`TradeReceiver.mq5`) to execute — market open/close/modify and pending-order place/modify/delete — instead of using MT5 IPC
- **Loop protection (`skip_own_magic`)**: when the master and a follower run on the SAME MT5 account (e.g. same-PC demo testing), the follower's own execution is re-broadcast to the hub with the follower's magic and would be copied again, looping forever. The agent skips any event whose magic equals its own magic (`skip_own_magic: true` by default; set `false` to disable). Master and follower must therefore use different magic numbers. This was verified live: a follower's own OPEN/CLOSE on the shared demo account was skipped while a real hub→agent order executed.
- **Risk limits**: per-follower `max_daily_loss`, `max_drawdown_pct`, and `max_daily_trades`; events blocked by a limit are queued on disk for later retry
- **Dashboard**: portfolio overview, equity history chart, per-account positions, activity log, and a settings tab to manage master, server, and follower config from the browser
- **Agent deployment**: an unconfigured agent (started with `--hub`) can receive a full config from the dashboard, which may also install MT5 automatically
- **SSE live updates**: dashboard values update in-place every ~2 s
- **PyInstaller build**: standalone `.exe` for Windows, no Python required

## Operational notes (verified live)

These were learned from live cross-machine testing (local master → remote follower
over the WebSocket hub, both demo and real Exness accounts):

- **Symbol variants on account groups**: Exness accounts expose symbols with a
  group suffix — demo trial accounts use `BTCUSDm`, real `c` accounts use
  `BTCUSDc` (plain `BTCUSD` returns no `symbol_info`). Map the master's symbol to
  the follower's variant with `symbol_mapping` (e.g. `{BTCUSD: BTCUSDc}`); the
  follower falls back to the master symbol name when no mapping matches.
- **First login needs one interactive session**: a brand-new MT5 install has no
  stored session, and the Python API cannot bootstrap one from nothing
  (`mt5.initialize` fails until the terminal has been logged in once). Start the
  terminal, log in manually once, then let the agent reuse the stored session.
- **Stale-event replay dedup**: on reconnect the hub replays missed events from
  the agent's `last_seq_id`. The follower now checks its position/order *history*
  (deals + orders, last 7 days, matching magic/comment) before executing: an open
  or place that already materialised, or a close/modify/delete whose target is no
  longer open, is silently skipped instead of re-executed. Verified live: stale
  hub replays produced `already materialised, skipping duplicate` no-ops while
  genuine hub events still executed. (The follower also skips its own
  re-broadcast trades via `skip_own_magic`.)
- **`max_positions` counts only the follower's own magic** — on a shared
  master+follower account the master's positions never consume the follower's
  copy cap.
- **Real-account caution**: this build was exercised on a live account
  (0.01 `BTCUSDc` open→close on `Exness-MT5Real25`, account 184073348): symbol
  mapping, comment correlation (master ticket), and close mirroring all verified
  end-to-end. Test on demo first, keep `lot_multiplier` small, and never point an
  agent at an account a strategy is actively trading without coordinating.
- **Known limits** (unchanged by the above):
  - Risk limits (`max_daily_loss`, `max_drawdown_pct`, `max_daily_trades`) are
    **not enforced in file-relay mode** (`terminal_data_path` set) — the agent
    cannot query positions via the API there. A one-time warning is logged at
    startup if limits are configured for a file-based agent.
  - `max_drawdown_pct` uses an in-memory daily peak equity that resets on
    restart, so a restart re-baselines the drawdown calculation.

## Security

- The REST API, WebSocket hub, and SSE stream have **no authentication**, and the server binds `0.0.0.0` by default. Anyone who can reach port 5000 can view the dashboard and change follower configuration — run only on a trusted network (LAN or a private Tailscale tailnet) and firewall the port.
- MT5 account passwords are stored in **plaintext** in `config.yaml` and `agent_config.yaml`. Protect these files with restrictive filesystem permissions, keep them out of shared folders and version control, and store agent configs only where the agent actually runs.

## Development

- **Layout**: `src/` (bridge, server/hub, config, state/EventStore, master monitor, EA signal tailer, follower executor, agent client, models), `static/` (dashboard HTML/JS), `tests/` (unit tests), `scratch/` (recovered debug artifacts)
- **Run tests**: `python -m unittest discover -s tests`
- **Verify syntax**: `python -m py_compile <changed files>`
- The `MetaTrader5` module is required at runtime but not importable in every environment — tests must mock it (e.g. via `sys.modules`)

## Requirements

- Python 3.12+
- MetaTrader 5 terminal (master + each follower)
- Tailscale for remote agents (or port forwarding)

## License

MIT
