"""Tests for src.bridge._event_to_dict serialization."""

from __future__ import annotations

import unittest

from helpers import install_mt5_mock

install_mt5_mock()  # bridge.py imports MetaTrader5 at module level

from src.bridge import _event_to_dict
from src.models import TradeEvent


def _event(**kw) -> TradeEvent:
    base = dict(action="open", symbol="XAUUSD", volume=0.5, price=100.0,
                sl=99.0, tp=101.0, master_ticket=1, position_type=0,
                comment="", magic=0)
    base.update(kw)
    return TradeEvent(**base)


class EventToDictTest(unittest.TestCase):
    def test_plain_open_event_shape(self):
        d = _event_to_dict(_event())
        self.assertEqual(d["action"], "open")
        self.assertEqual(d["symbol"], "XAUUSD")
        self.assertEqual(d["volume"], 0.5)
        self.assertEqual(d["price"], 100.0)
        self.assertEqual(d["sl"], 99.0)
        self.assertEqual(d["tp"], 101.0)
        self.assertEqual(d["master_ticket"], 1)
        self.assertEqual(d["position_type"], 0)
        self.assertEqual(d["comment"], "")
        self.assertEqual(d["magic"], 0)
        self.assertEqual(d["prev_volume"], None)
        self.assertNotIn("order_type", d)
        self.assertNotIn("expiration", d)

    def test_includes_order_type_and_expiration_when_set(self):
        d = _event_to_dict(_event(action="place", order_type=2, expiration=123456))
        self.assertEqual(d["order_type"], 2)
        self.assertEqual(d["expiration"], 123456)

    def test_omits_order_type_and_expiration_when_none(self):
        d = _event_to_dict(_event(action="place", order_type=None, expiration=None))
        self.assertNotIn("order_type", d)
        self.assertNotIn("expiration", d)

    def test_modify_event_includes_prev_volume(self):
        d = _event_to_dict(_event(action="modify", prev_volume=0.5))
        self.assertEqual(d["prev_volume"], 0.5)


if __name__ == "__main__":
    unittest.main()
