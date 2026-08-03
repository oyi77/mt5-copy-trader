"""Tests for src.models (TradeEvent / Position / PendingOrder)."""

from __future__ import annotations

import unittest

from src.models import PendingOrder, Position, TradeEvent

from helpers import fake_mt5_order, fake_mt5_position


class TradeEventVolumeChangeTest(unittest.TestCase):
    def _event(self, volume, prev_volume=None, **kw):
        return TradeEvent(
            action="modify", symbol="XAUUSD", volume=volume,
            price=100.0, sl=None, tp=None, master_ticket=1,
            position_type=0, comment="", magic=0,
            prev_volume=prev_volume, **kw,
        )

    def test_partial_close_delta(self):
        self.assertEqual(self._event(0.3, 0.5).volume_change(), 0.2)

    def test_delta_rounded_to_two_decimals(self):
        self.assertEqual(self._event(0.33, 1.5).volume_change(), 1.17)

    def test_full_close_delta(self):
        self.assertEqual(self._event(0.0, 0.5).volume_change(), 0.5)

    def test_no_prev_volume_returns_none(self):
        self.assertIsNone(self._event(0.3).volume_change())

    def test_volume_increase_returns_none(self):
        self.assertIsNone(self._event(0.7, 0.5).volume_change())

    def test_equal_volume_returns_none(self):
        self.assertIsNone(self._event(0.5, 0.5).volume_change())


class PositionFromMt5Test(unittest.TestCase):
    def test_maps_all_fields(self):
        p = Position.from_mt5(fake_mt5_position())
        self.assertEqual(p.ticket, 1)
        self.assertEqual(p.symbol, "XAUUSD")
        self.assertEqual(p.volume, 0.5)
        self.assertEqual(p.price_open, 100.0)
        self.assertEqual(p.price_current, 100.5)
        self.assertEqual(p.profit, 5.0)
        self.assertEqual(p.sl, 99.0)
        self.assertEqual(p.tp, 101.0)
        self.assertEqual(p.type, 0)
        self.assertEqual(p.comment, "")
        self.assertEqual(p.magic, 951000)

    def test_profit_defaults_to_zero_when_absent(self):
        pos = fake_mt5_position()
        del pos.profit
        self.assertEqual(Position.from_mt5(pos).profit, 0.0)


class PendingOrderFromMt5Test(unittest.TestCase):
    def test_maps_all_fields(self):
        o = PendingOrder.from_mt5(fake_mt5_order())
        self.assertEqual(o.ticket, 2)
        self.assertEqual(o.symbol, "EURUSD")
        self.assertEqual(o.volume, 1.0)   # volume_current
        self.assertEqual(o.price, 1.10)   # price_open
        self.assertEqual(o.sl, 0.0)
        self.assertEqual(o.tp, 0.0)
        self.assertEqual(o.type, 2)
        self.assertEqual(o.comment, "")
        self.assertEqual(o.magic, 951000)
        self.assertEqual(o.expiration, 0)

    def test_expiration_preserved(self):
        o = PendingOrder.from_mt5(fake_mt5_order(expiration=123456))
        self.assertEqual(o.expiration, 123456)

    def test_expiration_defaults_to_zero_when_absent(self):
        order = fake_mt5_order()
        del order.expiration
        self.assertEqual(PendingOrder.from_mt5(order).expiration, 0)


if __name__ == "__main__":
    unittest.main()
