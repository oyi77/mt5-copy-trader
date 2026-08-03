"""Master-side EA signal-file tailer — no MetaTrader5 dependency.

The master terminal runs ``TradeSender.mq5`` on any chart. That EA diffs the
account's open positions and pending orders every poll interval and appends
one pipe-delimited line per detected change to ``MQL5\\Files\\master_signals.txt``
inside the terminal's data folder.

This module tails that file and turns the lines into the same
:class:`~src.models.TradeEvent` objects :class:`~src.master.MasterMonitor`
produces — so the rest of the bridge pipeline (hub broadcast, EventStore,
follower execution) is unchanged — but without the MetaTrader5 package or the
IPC handshake that some Exness builds reject with error -6.

Line format (one per line, ANSI, ``SEQ`` strictly increasing):

    SEQ|<n>|OPEN|ticket|symbol|ptype|volume|price|sl|tp|comment|magic
    SEQ|<n>|CLOSE|ticket|symbol|ptype|volume|price|sl|tp|comment|magic
    SEQ|<n>|MODIFY|ticket|symbol|ptype|volume|price|sl|tp|comment|magic|prev_volume
    SEQ|<n>|PLACE|ticket|symbol|otype|volume|price|sl|tp|expiration|comment|magic
    SEQ|<n>|DELETE|ticket|symbol|otype|volume|price|sl|tp|expiration|comment|magic
    SEQ|<n>|MODIFY_ORDER|ticket|symbol|otype|volume|price|sl|tp|expiration|comment|magic|prev_volume
    SEQ|<n>|STATUS|login|name|balance|equity|margin|margin_free|leverage|currency|server
    SEQ|<n>|HEARTBEAT|<unix_seconds>

String fields (symbol, comment, account name, currency, server) are escaped
by the EA: ``\\`` becomes ``\\\\`` and ``|`` becomes ``\\|``. This module
unescapes while splitting, so field content may contain either character
freely.

The trailing ``|prev_volume`` on MODIFY / MODIFY_ORDER is present only when the
volume changed (partial close / partial fill), mirroring the IPC path.

Recovery semantics
------------------
The tailer persists the last processed ``SEQ`` to a sidecar JSON file next to
the signal file. On the first run (no sidecar) it baselines to the end of the
file, so pre-existing history is never re-broadcast — the same rule as
``MasterMonitor.snapshot()``. After that, lines with a higher ``SEQ`` are
replayed across bridge restarts, so events emitted while the bridge was down
are not lost. Duplicate execution is guarded downstream: followers skip opens
that already exist for a master ticket (comment ``copied_<ticket>``).

If the EA restarts within the same second its ``SEQ`` base resets, which would
normally make newer lines look like duplicates. The tailer detects this by
seeing a HEARTBEAT with a ``SEQ`` lower than the last processed one and
re-anchors, so no events are lost.
"""

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Optional

from src.models import TradeEvent

logger = logging.getLogger(__name__)

_ACTIONS = {"open", "close", "modify", "place", "delete", "modify_order"}


def _split_fields(line: str) -> list[str]:
    """Split a signal line on unescaped ``|`` and unescape each field.

    The EA escapes string fields (``\\`` -> ``\\\\``, ``|`` -> ``\\|``), so a
    naive ``str.split("|")`` would misalign fields whenever a comment, symbol
    or account name contains either character. A lone backslash not followed
    by ``\\`` or ``|`` is kept verbatim (e.g. Windows paths in comments).
    """
    fields: list[str] = []
    current: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] in ("\\", "|"):
            current.append(line[i + 1])
            i += 2
            continue
        if ch == "|":
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    fields.append("".join(current))
    return fields


def _opt_float(raw: str) -> Optional[float]:
    """Parse a float field; empty string and 0 become None (IPC semantics)."""
    if raw == "" or raw == "0":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return None if value == 0.0 else value


def _opt_int(raw: str) -> Optional[int]:
    """Parse an int field; empty string and 0 become None."""
    if raw == "" or raw == "0":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class MasterSignalFile:
    """Tail the EA signal file and produce TradeEvent objects.

    Thread-safety: the bridge calls this from a single thread (the bridge
    thread), so no locking is needed. The EA appends lines with open-close per
    write, and the tailer tolerates partial trailing lines.
    """

    def __init__(self, signals_path: str, heartbeat_timeout: float = 30.0):
        self._path = os.path.abspath(signals_path)
        self._state_path = self._path + ".state.json"
        self._heartbeat_timeout = heartbeat_timeout
        self._offset = 0
        self._buf = ""
        self._file_identity: Optional[tuple] = None
        self._last_seq = 0
        self._last_written_seq = -1
        self._baselined = False
        # True while re-reading old history after an offset reset (startup
        # resume, rotation, truncation). Old HEARTBEAT lines seen during such
        # a rescan must NOT be mistaken for an EA restart.
        self._history_scan = False
        self._last_activity = time.time()  # assume alive until proven otherwise
        self._known_tickets: set[int] = set()
        self._account: Optional[SimpleNamespace] = None

    # ------------------------------------------------------------------
    # Public API (same shape as MasterMonitor so the bridge can swap)
    # ------------------------------------------------------------------

    @property
    def known_tickets(self) -> set[int]:
        return self._known_tickets

    def connect(self) -> bool:
        """Initialize on bridge start: resume from sidecar, else baseline.

        Mirrors MasterMonitor.connect() so the bridge can treat both sources
        identically; file mode always succeeds (no IPC).
        """
        self.snapshot()
        return True

    def disconnect(self) -> None:
        """No-op for file mode."""

    def snapshot(self) -> None:
        """Baseline or resume on bridge start.

        First run (no sidecar): skip existing history and record the current
        SEQ — pre-existing positions are never relayed (the same rule as
        ``MasterMonitor.snapshot()``).

        Later runs (sidecar exists): resume from the persisted SEQ so events
        emitted while the bridge was down are not lost. The file is re-scanned
        from the top and the SEQ filter skips already-processed lines.
        """
        self._last_activity = time.time()
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._last_seq = int(data.get("seq", 0))
            self._baselined = True
            self._offset = 0
            self._buf = ""
            self._history_scan = True
            self._record_identity()
            logger.info("%s: EA master resuming from SEQ %d", self._path, self._last_seq)
            return
        except (OSError, ValueError, TypeError):
            pass

        # No sidecar — baseline to the end of the file so pre-existing
        # history (positions open before the bridge started) is not relayed.
        max_seq = 0
        try:
            if os.path.exists(self._path):
                with open(self._path, "rb") as f:
                    text = f.read().decode("utf-8", errors="replace")
                for line in text.splitlines():
                    parts = _split_fields(line)
                    if len(parts) >= 2 and parts[0] == "SEQ":
                        try:
                            seq = int(parts[1])
                        except ValueError:
                            continue
                        if seq > max_seq:
                            max_seq = seq
                    if len(parts) > 2 and parts[2] == "STATUS":
                        self._apply_status(parts)
                self._offset = os.path.getsize(self._path)
                self._buf = ""
        except OSError as e:
            logger.warning("%s: snapshot read failed: %s", self._path, e)
        self._record_identity()
        self._last_seq = max_seq
        self._baselined = True
        self._flush_state()
        logger.info("%s: EA master baseline established at SEQ %d", self._path, max_seq)

    def _record_identity(self) -> None:
        """Remember the current file identity so the next poll does not treat
        the re-read after a resume as a fresh rotation ('file replaced')."""
        try:
            st = os.stat(self._path)
            self._file_identity = (st.st_ino, st.st_ctime_ns)
        except OSError:
            self._file_identity = None

    def last_account(self) -> Optional[SimpleNamespace]:
        """Account info from the latest STATUS line, or None."""
        return self._account

    def poll_events(self) -> Optional[list[TradeEvent]]:
        """Return new TradeEvents since the last poll.

        Returns None when the EA is unreachable (signal file missing, or no
        heartbeat/status within the timeout) — the bridge treats that as
        ``master_connected = False``. Returns [] when the EA is alive but
        there is nothing new.
        """
        if not os.path.exists(self._path):
            logger.warning("%s: signal file missing — EA not running?", self._path)
            return None

        try:
            st = os.stat(self._path)
        except OSError as e:
            logger.warning("%s: stat failed: %s", self._path, e)
            return None

        identity = (st.st_ino, st.st_ctime_ns)
        if identity != self._file_identity:
            # File replaced (EA rotated it) — re-read from the top; the SEQ
            # filter prevents re-broadcast of already-processed lines.
            logger.info("%s: signal file replaced — re-reading from start", self._path)
            self._file_identity = identity
            self._offset = 0
            self._buf = ""
            self._history_scan = True
        elif st.st_size < self._offset:
            # Truncated in place — restart from the beginning as well.
            logger.info("%s: file truncated — re-reading from start", self._path)
            self._offset = 0
            self._buf = ""
            self._history_scan = True

        try:
            with open(self._path, "rb") as f:
                f.seek(self._offset)
                raw = f.read()
            self._offset = st.st_size
        except OSError as e:
            logger.warning("%s: read failed: %s", self._path, e)
            return None

        text = raw.decode("utf-8", errors="replace")
        if self._buf:
            text = self._buf + text
            self._buf = ""
        lines = text.split("\n")
        if text and not text.endswith("\n"):
            # Last segment has no newline yet — likely a partial trailing line.
            self._buf = lines.pop()
        elif lines and lines[-1] == "":
            lines.pop()

        if not self._baselined:
            self._baseline(lines)
            return []

        events: list[TradeEvent] = []
        for line in lines:
            self._process_line(line, events)
        self._history_scan = False
        self._flush_state()

        if time.time() - self._last_activity > self._heartbeat_timeout:
            logger.warning(
                "%s: EA heartbeat stale (>%.0fs) — treating master as unreachable",
                self._path, self._heartbeat_timeout,
            )
            return None
        return events

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _baseline(self, lines: list[str]) -> None:
        """First run: skip history, remember the highest SEQ seen."""
        max_seq = 0
        for line in lines:
            line = line.strip()
            parts = _split_fields(line)
            if len(parts) >= 2 and parts[0] == "SEQ":
                try:
                    seq = int(parts[1])
                except ValueError:
                    continue
                if seq > max_seq:
                    max_seq = seq
            if len(parts) > 2 and parts[2] == "STATUS":
                self._apply_status(parts)
        self._last_seq = max_seq
        self._last_activity = time.time()
        self._baselined = True
        self._flush_state()
        logger.info(
            "%s: baseline established at SEQ %d (%d historical lines skipped)",
            self._path, max_seq, len(lines),
        )

    def _process_line(self, line: str, events: list[TradeEvent]) -> None:
        line = line.strip()
        if not line:
            return
        parts = _split_fields(line)
        if len(parts) < 3 or parts[0] != "SEQ":
            logger.warning("%s: skipping malformed line: %.120s", self._path, line)
            return
        try:
            seq = int(parts[1])
        except ValueError:
            logger.warning("%s: bad SEQ in line: %.120s", self._path, line)
            return
        action = parts[2].lower()

        if seq <= self._last_seq:
            if action == "heartbeat" and seq < self._last_seq:
                if self._history_scan:
                    # Old history being re-scanned after an offset reset — the
                    # low SEQ is expected, not an EA restart.
                    return
                # EA restarted (its SEQ base = unix seconds reset). Re-anchor so
                # events emitted after the restart are not treated as duplicates.
                logger.info(
                    "%s: EA restart detected (HEARTBEAT SEQ %d < last %d) — re-anchoring",
                    self._path, seq, self._last_seq,
                )
                self._last_seq = seq - 1
            else:
                return  # duplicate / already processed

        if action == "heartbeat":
            self._last_activity = time.time()
            self._last_seq = seq
            return
        if action == "status":
            self._apply_status(parts)
            self._last_activity = time.time()
            self._last_seq = seq
            return

        event = self._parse_trade_action(action, parts)
        if event is None:
            logger.warning("%s: unknown action %r — skipping line", self._path, action)
            return
        self._last_seq = seq
        self._last_activity = time.time()
        if event.action in ("open", "place"):
            self._known_tickets.add(event.master_ticket)
        events.append(event)

    def _parse_trade_action(self, action: str, parts: list[str]) -> Optional[TradeEvent]:
        if action in ("open", "close", "modify"):
            if len(parts) < 12:
                logger.warning("%s: %s line has %d fields, need 12", self._path, action, len(parts))
                return None
            prev_volume = (
                _opt_float(parts[12])
                if action == "modify" and len(parts) > 12
                else None
            )
            return TradeEvent(
                action=action,
                symbol=parts[4],
                volume=float(parts[6]),
                price=float(parts[7]),
                sl=_opt_float(parts[8]),
                tp=_opt_float(parts[9]),
                master_ticket=int(parts[3]),
                position_type=int(parts[5]),
                comment=parts[10],
                magic=int(parts[11]),
                prev_volume=prev_volume,
            )
        if action in ("place", "delete", "modify_order"):
            if len(parts) < 13:
                logger.warning("%s: %s line has %d fields, need 13", self._path, action, len(parts))
                return None
            otype = int(parts[5])
            prev_volume = (
                _opt_float(parts[13])
                if action == "modify_order" and len(parts) > 13
                else None
            )
            return TradeEvent(
                action=action,
                symbol=parts[4],
                volume=float(parts[6]),
                price=float(parts[7]),
                sl=_opt_float(parts[8]),
                tp=_opt_float(parts[9]),
                master_ticket=int(parts[3]),
                position_type=otype,
                comment=parts[11],
                magic=int(parts[12]),
                order_type=otype,
                expiration=_opt_int(parts[10]),
                prev_volume=prev_volume,
            )
        return None

    def _apply_status(self, parts: list[str]) -> None:
        try:
            account = SimpleNamespace(
                login=int(parts[3]),
                name=parts[4],
                balance=float(parts[5]),
                equity=float(parts[6]),
                margin=float(parts[7]),
                margin_free=float(parts[8]),
                leverage=int(parts[9]),
                currency=parts[10],
                server=parts[11],
            )
        except (ValueError, IndexError):
            logger.warning("%s: malformed STATUS line: %.120s", self._path, "|".join(parts))
            return
        self._account = account

    def _flush_state(self) -> None:
        if self._last_seq == self._last_written_seq:
            return
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seq": self._last_seq}, f)
            os.replace(tmp, self._state_path)
            self._last_written_seq = self._last_seq
        except OSError as e:
            logger.warning("%s: failed to persist SEQ state: %s", self._state_path, e)
