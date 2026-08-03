#!/usr/bin/env python3
"""Weekly audit report for the copy-trade engine.

Reads the SQLite event store (event_store.db) and the engine's log file
(logs/copy_trade.log) and produces a plain report covering:

  - Uptime % : how much of the window the master bridge was actually running
  - Signals  : trade events by action, by symbol, and per-day
  - Errors   : ERROR-level log lines within the window
  - Gaps     : silence periods between consecutive events longer than a
               configured threshold (possible missed-signal indicators)

This is the "prove I'm reliable" artifact: a weekly summary independent of
the dashboard. It only READS runtime data and opens the SQLite store
read-only, so it is safe to run while the engine is live.

Usage:
    python audit_report.py [--days 7] [--db event_store.db]
                           [--log logs/copy_trade.log] [--gap-minutes 60]
                           [--out report.md]
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

# Fix Windows console encoding for unicode output (same pattern as run.py)
if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Matches the engine's RotatingFileHandler format:
#   2026-08-01 15:36:05 | INFO  | src.bridge       | BRIDGE - starting
_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| ([A-Z]+) +\| .+? \| (.*)$"
)

BRIDGE_START_MARKER = "BRIDGE - starting"
BRIDGE_STOP_MARKER = "BRIDGE STOPPED"


def _to_ts(text: str) -> float:
    """Convert a log timestamp (YYYY-MM-DD HH:MM:SS) to a unix timestamp."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def parse_log(
    log_path: str,
    start_ts: float,
    end_ts: float,
) -> tuple[list[tuple[float, float]], int, int, dict]:
    """Parse the engine log for bridge sessions, errors, and top error types.

    Returns (sessions, errors, total_lines, errors_by_message). Each session
    is a (start_ts, end_ts) interval during which the bridge was running,
    clipped to those that overlap the window. A session without a stop
    marker extends to end_ts (still running). Error counts and messages are
    only recorded for lines within the window; the whole file is parsed so
    a session that started before the window is still counted for uptime.
    """
    sessions: list[tuple[float, float]] = []
    errors = 0
    total_lines = 0
    errors_by_message: Counter = Counter()
    open_start: Optional[float] = None

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _LOG_LINE.match(line)
                if not m:
                    continue
                total_lines += 1
                ts = _to_ts(m.group(1))
                if ts > end_ts:
                    break
                level, message = m.group(2), m.group(3).strip()
                if level == "ERROR" and ts >= start_ts:
                    errors += 1
                    errors_by_message[message[:120]] += 1
                if message == BRIDGE_START_MARKER:
                    open_start = ts
                elif message == BRIDGE_STOP_MARKER and open_start is not None:
                    if ts >= start_ts:  # session overlaps the window
                        sessions.append((open_start, ts))
                    open_start = None
    except FileNotFoundError:
        return sessions, errors, 0, {}

    if open_start is not None:
        sessions.append((open_start, end_ts))
    return sessions, errors, total_lines, dict(errors_by_message)


def _uptime_percent(
    sessions: list[tuple[float, float]], start_ts: float, end_ts: float
) -> float:
    """Fraction of the window covered by bridge sessions."""
    window = max(end_ts - start_ts, 1.0)
    covered = 0.0
    for s, e in sessions:
        covered += max(0.0, min(e, end_ts) - max(s, start_ts))
    return min(covered / window * 100.0, 100.0)


def load_events(db_path: str, start_ts: float, end_ts: float) -> list[dict]:
    """Read events from the store within the window (oldest first)."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.execute(
            """SELECT action, symbol, volume, price, master_ticket,
                      position_type, comment, magic, created_at
               FROM events WHERE created_at >= ? AND created_at <= ?
               ORDER BY created_at ASC""",
            (start_ts, end_ts),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def compute_gaps(
    events: list[dict],
    sessions: list[tuple[float, float]],
    min_gap_s: float,
) -> list[dict]:
    """Find silence periods between consecutive events exceeding min_gap_s.

    A gap is discarded when a bridge session starts inside it (that is a
    restart, not a missed-signal window).
    """
    gaps: list[dict] = []
    for prev, cur in zip(events, events[1:]):
        delta = cur["created_at"] - prev["created_at"]
        if delta < min_gap_s:
            continue
        restart = any(s > prev["created_at"] + 1 and s < cur["created_at"] for s, _ in sessions)
        if restart:
            continue
        gaps.append({
            "from_ts": prev["created_at"],
            "to_ts": cur["created_at"],
            "to_action": cur["action"],
            "to_symbol": cur["symbol"],
            "minutes": round(delta / 60.0, 1),
        })
    gaps.sort(key=lambda g: g["minutes"], reverse=True)
    return gaps


def generate_report(
    db_path: str = "event_store.db",
    log_path: str = "logs/copy_trade.log",
    days: int = 7,
    gap_minutes: int = 60,
) -> dict:
    """Build the structured report data for the window."""
    end_ts = time.time()
    start_ts = end_ts - days * 86400.0

    sessions, errors, total_log_lines, errors_by_message = parse_log(log_path, start_ts, end_ts)
    events = load_events(db_path, start_ts, end_ts)
    gaps = compute_gaps(events, sessions, gap_minutes * 60.0)

    by_action = Counter(e["action"] for e in events)
    by_symbol = Counter(e["symbol"] for e in events)
    per_day: Counter = Counter()
    for e in events:
        day = datetime.fromtimestamp(e["created_at"]).strftime("%Y-%m-%d")
        per_day[f"{day} {e['action']}"] += 1

    last_event_ts = events[-1]["created_at"] if events else None
    return {
        "window": {"start": start_ts, "end": end_ts, "days": days},
        "sessions": sessions,
        "uptime_percent": round(_uptime_percent(sessions, start_ts, end_ts), 2),
        "log": {
            "path": log_path,
            "lines": total_log_lines,
            "errors": errors,
            "errors_by_message": errors_by_message,
        },
        "events": {
            "total": len(events),
            "by_action": dict(by_action),
            "by_symbol": dict(by_symbol),
            "per_day": dict(per_day),
        },
        "gaps": gaps,
        "last_event_ts": last_event_ts,
    }


def render_markdown(data: dict) -> str:
    """Render the report as a markdown document."""
    start = datetime.fromtimestamp(data["window"]["start"])
    end = datetime.fromtimestamp(data["window"]["end"])
    ev = data["events"]
    lines = [
        "# Copy-Trade Engine — Weekly Audit",
        "",
        f"- Report generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- Window: {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} ({data['window']['days']} days)",
        f"- Bridge uptime: **{data['uptime_percent']:.2f}%** "
        f"({len(data['sessions'])} session(s))",
        f"- Log: `{data['log']['path']}` ({data['log']['lines']} parsed lines, "
        f"**{data['log']['errors']} errors**)",
        "",
    ]
    if data["log"]["errors_by_message"]:
        lines.append("### Top error messages")
        lines.append("")
        lines.append("| Count | Message |")
        lines.append("|---|---|")
        for msg, count in sorted(
            data["log"]["errors_by_message"].items(), key=lambda kv: -kv[1]
        )[:5]:
            lines.append(f"| {count} | `{msg}` |")
        lines.append("")
    lines += [
        "## Signals",
        "",
        f"Total trade events: **{ev['total']}**",
        "",
    ]
    if ev["by_action"]:
        lines.append("| Action | Count |")
        lines.append("|---|---|")
        for action, count in sorted(ev["by_action"].items()):
            lines.append(f"| {action} | {count} |")
        lines.append("")
    if ev["by_symbol"]:
        lines.append("| Symbol | Count |")
        lines.append("|---|---|")
        for symbol, count in sorted(ev["by_symbol"].items()):
            lines.append(f"| {symbol} | {count} |")
        lines.append("")
    if ev["per_day"]:
        lines.append("### Per day")
        lines.append("")
        lines.append("| Day + action | Count |")
        lines.append("|---|---|")
        for key, count in sorted(ev["per_day"].items()):
            lines.append(f"| {key} | {count} |")
        lines.append("")
    if data["gaps"]:
        lines.append(f"## Gaps (> {data['window']['days']} day check, longest first)")
        lines.append("")
        lines.append("Silence between consecutive signals — inspect for missed trades:")
        lines.append("")
        lines.append("| From | To | Minutes | Next signal |")
        lines.append("|---|---|---|---|")
        for g in data["gaps"][:20]:
            lines.append(
                f"| {datetime.fromtimestamp(g['from_ts']):%m-%d %H:%M} "
                f"| {datetime.fromtimestamp(g['to_ts']):%m-%d %H:%M} "
                f"| {g['minutes']:.0f} | {g['to_action']} {g['to_symbol']} |"
            )
        lines.append("")
    else:
        lines.append("## Gaps")
        lines.append("")
        lines.append("None above the threshold. ✅")
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Per-subscriber fill data is not persisted in `event_store.db`;")
    lines.append("  subscriber-level execution results are only visible in agent logs.")
    lines.append("- Gaps spanning a bridge restart are excluded (they are restarts,")
    lines.append("  not missed-signal windows).")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="event_store.db", help="SQLite event store path")
    parser.add_argument("--log", default="logs/copy_trade.log", help="Engine log path")
    parser.add_argument("--days", type=int, default=7, help="Report window in days (default 7)")
    parser.add_argument("--gap-minutes", type=int, default=60, help="Gap threshold in minutes")
    parser.add_argument("--out", default="", help="Optional output file (.md); default: stdout")
    args = parser.parse_args()

    data = generate_report(args.db, args.log, args.days, args.gap_minutes)
    markdown = render_markdown(data)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"Wrote audit report to {args.out}")
    else:
        print(markdown)


if __name__ == "__main__":
    main()
