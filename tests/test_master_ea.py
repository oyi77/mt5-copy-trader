"""Tests for src.master_ea.MasterSignalFile — EA signal-file tailer."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from src.master_ea import MasterSignalFile


def _write(path: str, *lines: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


class MasterSignalFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="master_ea_test_")
        self.signals = os.path.join(self.dir, "master_signals.txt")
        self.tailer = MasterSignalFile(self.signals, heartbeat_timeout=30.0)

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    # ── Baseline / resume ─────────────────────────────────────────

    def test_first_run_baselines_history(self):
        _write(self.signals,
               "SEQ|100|STATUS|433903489|Demo|1000.00|1000.00|0.00|1000.00|500|USD|Exness-MT5Trial7",
               "SEQ|101|OPEN|111|BTCUSD|0|0.01|67000.5|0|0|copied_111|0")
        self.tailer.snapshot()
        # Historical lines must never be re-broadcast
        self.assertEqual(self.tailer.poll_events(), [])
        self.assertEqual(self.tailer.poll_events(), [])

    def test_resume_from_persisted_seq(self):
        _write(self.signals, "SEQ|100|OPEN|111|BTCUSD|0|0.01|67000.5|0|0|copied_111|0")
        self.tailer.snapshot()
        self.tailer.poll_events()  # flushes sidecar

        # New tailer on the same file = bridge restart
        tailer2 = MasterSignalFile(self.signals)
        tailer2.snapshot()
        events = tailer2.poll_events()
        self.assertEqual(events, [])  # old line not re-emitted

        _write(self.signals, "SEQ|102|CLOSE|111|BTCUSD|0|0.01|67100.0|0|0|copied_111|0")
        events = tailer2.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "close")
        self.assertEqual(events[0].master_ticket, 111)

    def test_baseline_skips_existing_history_without_snapshot(self):
        # Standalone use: first poll (no snapshot called) also baselines
        _write(self.signals, "SEQ|5|OPEN|1|XAUUSD|0|0.10|2000.0|0|0|copy|0")
        self.assertEqual(self.tailer.poll_events(), [])
        _write(self.signals, "SEQ|6|OPEN|2|XAUUSD|1|0.10|2010.0|0|0|copy|0")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].master_ticket, 2)

    # ── Parsing ───────────────────────────────────────────────────

    def test_parse_open_event(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals, "SEQ|11|OPEN|42|BTCUSD|0|0.05|67000.12345|66500|68000|hello|951001")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "open")
        self.assertEqual(e.symbol, "BTCUSD")
        self.assertEqual(e.volume, 0.05)
        self.assertEqual(e.price, 67000.12345)
        self.assertEqual(e.sl, 66500.0)
        self.assertEqual(e.tp, 68000.0)
        self.assertEqual(e.master_ticket, 42)
        self.assertEqual(e.position_type, 0)
        self.assertEqual(e.comment, "hello")
        self.assertEqual(e.magic, 951001)
        self.assertIsNone(e.prev_volume)

    def test_parse_modify_with_prev_volume(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals, "SEQ|11|MODIFY|42|BTCUSD|0|0.03|67000.0|66500|68000|hello|951001|0.05")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "modify")
        self.assertEqual(events[0].volume, 0.03)
        self.assertEqual(events[0].prev_volume, 0.05)

    def test_modify_without_prev_volume(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals, "SEQ|11|MODIFY|42|BTCUSD|0|0.05|67000.0|66500|68000|hello|951001")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].prev_volume)

    def test_parse_order_events(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals,
               "SEQ|11|PLACE|50|BTCUSD|2|0.10|65000|64500|66000|1800000000|limit1|951001",
               "SEQ|12|MODIFY_ORDER|50|BTCUSD|2|0.10|64800|64500|66000|1800000000|limit1|951001|0.20")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].action, "place")
        self.assertEqual(events[0].order_type, 2)
        self.assertEqual(events[0].position_type, 2)
        self.assertEqual(events[0].expiration, 1800000000)
        self.assertEqual(events[1].action, "modify_order")
        self.assertEqual(events[1].prev_volume, 0.20)

    def test_zero_sl_tp_becomes_none(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals, "SEQ|11|CLOSE|42|BTCUSD|0|0.05|67000.0|0|0|copied_42|0")
        e = self.tailer.poll_events()[0]
        self.assertIsNone(e.sl)
        self.assertIsNone(e.tp)

    def test_status_updates_account(self):
        _write(self.signals,
               "SEQ|1|STATUS|433903489|My Demo|1234.50|1250.00|10.00|1240.00|500|USD|Exness-MT5Trial7")
        self.tailer.snapshot()
        acc = self.tailer.last_account()
        self.assertIsNotNone(acc)
        self.assertEqual(acc.login, 433903489)
        self.assertEqual(acc.balance, 1234.50)
        self.assertEqual(acc.equity, 1250.00)
        self.assertEqual(acc.margin, 10.00)
        self.assertEqual(acc.margin_free, 1240.00)
        self.assertEqual(acc.leverage, 500)
        self.assertEqual(acc.currency, "USD")
        self.assertEqual(acc.server, "Exness-MT5Trial7")

    # ── Liveness / recovery ───────────────────────────────────────

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.tailer.poll_events())

    def test_stale_heartbeat_returns_none(self):
        with mock.patch("src.master_ea.time.time", return_value=1000.0):
            tailer = MasterSignalFile(self.signals, heartbeat_timeout=10.0)
            _write(self.signals, "SEQ|1|HEARTBEAT|1000")
            tailer.snapshot()  # stamps _last_activity = 1000
            self.assertEqual(tailer.poll_events(), [])
        with mock.patch("src.master_ea.time.time", return_value=1000.0 + 31.0):
            # 31s since last heartbeat — beyond the 10s timeout
            self.assertIsNone(tailer.poll_events())

    def test_heartbeat_reanchors_after_ea_restart(self):
        _write(self.signals, "SEQ|100|OPEN|111|BTCUSD|0|0.01|67000|0|0|copied_111|0")
        self.tailer.snapshot()
        self.assertEqual(len(self.tailer.poll_events()), 0)  # baseline history

        # EA restarted within the same second: SEQ base reset to a lower value
        _write(self.signals,
               "SEQ|50|HEARTBEAT|1000",          # lower than last — restart marker
               "SEQ|51|CLOSE|111|BTCUSD|0|0.01|67100|0|0|copied_111|0")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "close")

    def test_rescan_history_does_not_reanchor(self):
        # File contains an old heartbeat below the persisted SEQ — a normal
        # occurrence when history is re-scanned after a bridge restart.
        _write(self.signals,
               "SEQ|50|HEARTBEAT|1000",
               "SEQ|51|OPEN|111|BTCUSD|0|0.01|67000|0|0|copied_111|0",
               "SEQ|100|OPEN|222|BTCUSD|0|0.02|67200|0|0|copied_222|0")
        self.tailer.snapshot()
        self.assertEqual(self.tailer.poll_events(), [])  # baseline → sidecar 100

        # Bridge restart: resume from sidecar (seq=100), file re-scanned from top.
        tailer2 = MasterSignalFile(self.signals)
        tailer2.snapshot()
        # The old heartbeat must NOT be misread as an EA restart, and the
        # already-processed opens must NOT be re-broadcast.
        self.assertEqual(tailer2.poll_events(), [])

        # Live data after the rescan still flows.
        _write(self.signals, "SEQ|101|CLOSE|111|BTCUSD|0|0.01|67100|0|0|copied_111|0")
        events = tailer2.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "close")

    def test_snapshot_records_file_identity(self):
        _write(self.signals, "SEQ|1|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        st = os.stat(self.signals)
        self.assertEqual(self.tailer._file_identity, (st.st_ino, st.st_ctime_ns))
        # First poll must not treat the same file as 'replaced'
        self.assertEqual(self.tailer.poll_events(), [])
        self.assertEqual(self.tailer._file_identity, (st.st_ino, st.st_ctime_ns))

    def test_resume_records_file_identity(self):
        _write(self.signals, "SEQ|1|OPEN|111|BTCUSD|0|0.01|67000|0|0|copied_111|0")
        self.tailer.snapshot()
        self.tailer.poll_events()  # flush sidecar
        tailer2 = MasterSignalFile(self.signals)
        tailer2.snapshot()  # resume path — offset reset, identity must be recorded
        st = os.stat(self.signals)
        self.assertEqual(tailer2._file_identity, (st.st_ino, st.st_ctime_ns))
        self.assertEqual(tailer2.poll_events(), [])

    def test_partial_trailing_line_is_buffered(self):
        _write(self.signals, "SEQ|1|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        # No trailing newline — partial line
        with open(self.signals, "a", encoding="utf-8") as f:
            f.write("SEQ|2|OPEN|9|BTCUSD|0|0.01|67000|0|0|copied_9|0")  # no \n
        self.assertEqual(self.tailer.poll_events(), [])
        # Rest of the line arrives
        _write(self.signals, "")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].master_ticket, 9)

    def test_malformed_lines_skipped(self):
        _write(self.signals,
               "SEQ|1|STATUS|1|A|100|100|0|100|100|USD|S",
               "not a valid line",
               "SEQ|notanum|OPEN|x|y|0|1|1|0|0|c|0",
               "SEQ|2|BOGUS|1|2|3|4|5|6|7|8|9")
        self.tailer.snapshot()
        _write(self.signals, "SEQ|3|OPEN|7|BTCUSD|0|0.01|67000|0|0|copied_7|0")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].master_ticket, 7)

    def test_rotation_does_not_rebroadcast(self):
        _write(self.signals, "SEQ|1|OPEN|111|BTCUSD|0|0.01|67000|0|0|copied_111|0")
        self.tailer.snapshot()
        self.assertEqual(self.tailer.poll_events(), [])
        # EA rotates: deletes file and starts fresh with a higher base
        os.remove(self.signals)
        _write(self.signals, "SEQ|1000|OPEN|222|BTCUSD|1|0.02|67500|0|0|copied_222|0")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].master_ticket, 222)

    def test_known_tickets_tracks_opens(self):
        _write(self.signals, "SEQ|1|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals,
               "SEQ|2|OPEN|111|BTCUSD|0|0.01|67000|0|0|copied_111|0",
               "SEQ|3|OPEN|222|BTCUSD|1|0.02|67500|0|0|copied_222|0")
        self.tailer.poll_events()
        self.assertEqual(self.tailer.known_tickets, {111, 222})

    def test_sidecar_persists_seq(self):
        _write(self.signals, "SEQ|1|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        self.tailer.poll_events()
        with open(self.signals + ".state.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["seq"], 1)

    # ── Escaping (EA writes '\' -> '\\' and '|' -> '\|') ─────────────

    def test_escaped_pipe_in_comment(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        # The EA escapes the pipe in the comment: a\|b in the file = "a|b"
        _write(self.signals, "SEQ|11|OPEN|42|BTCUSD|0|0.05|67000|0|0|a\\|b|951001")
        events = self.tailer.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].comment, "a|b")
        self.assertEqual(events[0].magic, 951001)

    def test_escaped_backslash_in_comment(self):
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        # The EA escapes backslash: C:\\Temp in the file = "C:\Temp"
        _write(self.signals, "SEQ|11|OPEN|42|BTCUSD|0|0.05|67000|0|0|C:\\\\Temp|951001")
        e = self.tailer.poll_events()[0]
        self.assertEqual(e.comment, "C:\\Temp")

    def test_escaped_pipe_in_account_name(self):
        _write(self.signals, "SEQ|1|STATUS|1|My\\|Demo|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        acc = self.tailer.last_account()
        self.assertEqual(acc.name, "My|Demo")

    def test_lone_backslash_kept_verbatim(self):
        # A backslash NOT followed by '\' or '|' is not an escape sequence
        _write(self.signals, "SEQ|10|STATUS|1|A|100|100|0|100|100|USD|S")
        self.tailer.snapshot()
        _write(self.signals, "SEQ|11|OPEN|42|BTCUSD|0|0.05|67000|0|0|C:\\Temp\\x|951001")
        e = self.tailer.poll_events()[0]
        self.assertEqual(e.comment, "C:\\Temp\\x")


if __name__ == "__main__":
    unittest.main()
