"""Tests for audit_report.py."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from audit_report import generate_report

HOUR = 3600.0


class AuditReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "events.db"
        self.log = self.root / "engine.log"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                volume REAL NOT NULL DEFAULT 0.0,
                price REAL NOT NULL DEFAULT 0.0,
                sl REAL, tp REAL,
                master_ticket INTEGER NOT NULL DEFAULT 0,
                position_type INTEGER NOT NULL DEFAULT 0,
                comment TEXT NOT NULL DEFAULT '',
                magic INTEGER NOT NULL DEFAULT 0,
                prev_volume REAL, order_type INTEGER, expiration INTEGER,
                created_at REAL NOT NULL
            );
            """
        )
        conn.commit()
        self.conn = conn

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _insert(self, action: str, symbol: str, ts: float) -> None:
        self.conn.execute(
            "INSERT INTO events (action, symbol, volume, price, master_ticket,"
            " position_type, comment, magic, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (action, symbol, 0.01, 1.0, 0, 0, "", 0, ts),
        )
        self.conn.commit()

    def _write_log(self, lines: list[str]) -> None:
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _line(ts: float, level: str, msg: str) -> str:
        stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        return f"{stamp} | {level:<5} | src.bridge       | {msg}"

    def test_counts_and_uptime(self) -> None:
        now = time.time()
        start = now - 3 * HOUR
        stop = now - 2 * HOUR
        self._write_log([
            self._line(start, "INFO", "BRIDGE - starting"),
            self._line(start + 600, "ERROR", "Master connect failed: (-6, auth)"),
            self._line(stop, "INFO", "BRIDGE STOPPED"),
        ])
        self._insert("open", "BTCUSD", start + 1200)
        self._insert("close", "BTCUSD", start + 2400)

        data = generate_report(str(self.db), str(self.log), days=1, gap_minutes=60)
        self.assertEqual(data["events"]["total"], 2)
        self.assertEqual(data["events"]["by_action"], {"open": 1, "close": 1})
        self.assertEqual(data["events"]["by_symbol"], {"BTCUSD": 2})
        self.assertEqual(data["log"]["errors"], 1)
        self.assertEqual(data["log"]["errors_by_message"],
                         {"Master connect failed: (-6, auth)": 1})
        # bridge ran 1h within a 24h window -> ~4.17%
        self.assertAlmostEqual(data["uptime_percent"], 100.0 * HOUR / (24 * HOUR), places=1)

    def test_session_straddling_window_start_is_counted(self) -> None:
        now = time.time()
        # Session starts 25h ago (before the 24h window) and stops 23h ago
        start = now - 25 * HOUR
        stop = now - 23 * HOUR
        self._write_log([
            self._line(start, "INFO", "BRIDGE - starting"),
            self._line(stop, "INFO", "BRIDGE STOPPED"),
        ])
        data = generate_report(str(self.db), str(self.log), days=1, gap_minutes=60)
        # clipped to the window: 1h of 24h counted
        self.assertAlmostEqual(data["uptime_percent"], 100.0 * HOUR / (24 * HOUR), places=1)

    def test_gap_detected_and_restart_excluded(self) -> None:
        now = time.time()
        t1 = now - 3 * HOUR
        t2 = now - 1 * HOUR  # 2h later -> gap
        self._insert("open", "BTCUSD", t1)
        self._insert("close", "BTCUSD", t2)

        # No log file at all -> no sessions -> gap kept
        data = generate_report(str(self.db), str(self.log), days=1, gap_minutes=60)
        self.assertEqual(len(data["gaps"]), 1)
        self.assertAlmostEqual(data["gaps"][0]["minutes"], 120.0, places=0)

        # A bridge restart inside the gap excludes it
        self._write_log([self._line(t1 + HOUR, "INFO", "BRIDGE - starting")])
        data2 = generate_report(str(self.db), str(self.log), days=1, gap_minutes=60)
        self.assertEqual(data2["gaps"], [])

    def test_empty_store(self) -> None:
        data = generate_report(str(self.db), str(self.log), days=7, gap_minutes=60)
        self.assertEqual(data["events"]["total"], 0)
        self.assertEqual(data["gaps"], [])
        self.assertEqual(data["log"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
