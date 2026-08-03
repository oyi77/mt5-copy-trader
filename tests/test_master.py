"""Tests for src.master.MasterMonitor change detection."""

from __future__ import annotations

import unittest

from helpers import install_mt5_mock

install_mt5_mock()  # master.py imports MetaTrader5 at module level

from src.config import MasterConfig
from src.master import MasterMonitor
from src.models import PendingOrder, Position


def _pos(**kw) -> Position:
    base = dict(ticket=1, symbol="XAUUSD", volume=0.5, price_open=100.0,
                price_current=100.5, profit=5.0, sl=99.0, tp=101.0,
                type=0, comment="", magic=0)
    base.update(kw)
    return Position(**base)


def _order(**kw) -> PendingOrder:
    base = dict(ticket=10, symbol="EURUSD", volume=1.0, price=1.10,
                sl=0.0, tp=0.0, type=2, comment="", magic=0, expiration=0)
    base.update(kw)
    return PendingOrder(**base)


def _monitor() -> MasterMonitor:
    return MasterMonitor(MasterConfig(path="C:/MT5/t.exe", port=15555))


class DetectChangesTest(unittest.TestCase):
    def test_new_position_emits_open(self):
        mon = _monitor()
        events = mon.detect_changes([_pos()])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "open")
        self.assertEqual(e.symbol, "XAUUSD")
        self.assertEqual(e.volume, 0.5)
        self.assertEqual(e.price, 100.0)  # price_open
        self.assertEqual(e.sl, 99.0)
        self.assertEqual(e.tp, 101.0)
        self.assertEqual(e.master_ticket, 1)
        self.assertEqual(e.position_type, 0)
        self.assertEqual(e.comment, "")
        self.assertEqual(e.magic, 0)
        self.assertEqual(mon.known_tickets, {1})

    def test_duplicate_new_position_suppressed(self):
        mon = _monitor()
        self.assertEqual(len(mon.detect_changes([_pos()])), 1)
        self.assertEqual(mon.detect_changes([_pos()]), [])

    def test_removed_position_emits_close(self):
        mon = _monitor()
        mon.detect_changes([_pos()])  # open -> known_tickets
        events = mon.detect_changes([])  # position gone
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "close")
        self.assertEqual(e.master_ticket, 1)
        self.assertEqual(e.price, 100.5)  # price_current
        self.assertEqual(mon.known_tickets, set())

    def test_snapshot_positions_never_emit(self):
        mon = _monitor()
        mon.snapshot([_pos()])
        # unchanged snapshot position
        self.assertEqual(mon.detect_changes([_pos()]), [])
        # SL/TP change on a snapshot position: no event
        self.assertEqual(mon.detect_changes([_pos(sl=88.0)]), [])
        # removal after snapshot: no close (never in known_tickets)
        self.assertEqual(mon.detect_changes([]), [])

    def test_modify_sl_tp_emits_modify_without_prev_volume(self):
        mon = _monitor()
        mon.detect_changes([_pos()])
        events = mon.detect_changes([_pos(sl=90.0, tp=110.0)])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "modify")
        self.assertEqual(e.sl, 90.0)
        self.assertEqual(e.tp, 110.0)
        self.assertIsNone(e.prev_volume)

    def test_modify_volume_emits_prev_volume(self):
        mon = _monitor()
        mon.detect_changes([_pos()])
        events = mon.detect_changes([_pos(volume=0.3)])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "modify")
        self.assertEqual(e.volume, 0.3)
        self.assertEqual(e.prev_volume, 0.5)

    def test_volume_and_sl_change_single_modify_event(self):
        mon = _monitor()
        mon.detect_changes([_pos()])
        events = mon.detect_changes([_pos(volume=0.3, sl=90.0)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].prev_volume, 0.5)

    def test_no_change_emits_nothing(self):
        mon = _monitor()
        mon.detect_changes([_pos()])
        self.assertEqual(mon.detect_changes([_pos()]), [])

    def test_zero_sl_tp_maps_to_none(self):
        mon = _monitor()
        events = mon.detect_changes([_pos(sl=0.0, tp=0.0)])
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].sl)
        self.assertIsNone(events[0].tp)


class DetectOrderChangesTest(unittest.TestCase):
    def test_new_order_emits_place(self):
        mon = _monitor()
        events = mon.detect_order_changes([_order()])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "place")
        self.assertEqual(e.order_type, 2)
        self.assertEqual(e.master_ticket, 10)
        self.assertIsNone(e.expiration)  # expiration=0 -> None

    def test_place_includes_expiration(self):
        mon = _monitor()
        events = mon.detect_order_changes([_order(expiration=123456)])
        self.assertEqual(events[0].action, "place")
        self.assertEqual(events[0].expiration, 123456)

    def test_duplicate_order_suppressed(self):
        mon = _monitor()
        self.assertEqual(len(mon.detect_order_changes([_order()])), 1)
        self.assertEqual(mon.detect_order_changes([_order()]), [])

    def test_removed_order_emits_delete(self):
        mon = _monitor()
        mon.detect_order_changes([_order()])  # place
        events = mon.detect_order_changes([])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "delete")
        self.assertEqual(e.order_type, 2)

    def test_modified_order_emits_modify_order(self):
        mon = _monitor()
        mon.detect_order_changes([_order()])
        events = mon.detect_order_changes([_order(price=1.15)])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.action, "modify_order")
        self.assertEqual(e.price, 1.15)
        self.assertEqual(e.order_type, 2)
        self.assertIsNone(e.expiration)

    def test_snapshot_orders_never_emit(self):
        mon = _monitor()
        mon.snapshot([], [_order()])
        self.assertEqual(mon.detect_order_changes([_order()]), [])
        self.assertEqual(mon.detect_order_changes([]), [])


if __name__ == "__main__":
    unittest.main()
