"""Tests for src.bridge._event_to_dict serialization."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from helpers import install_mt5_mock

install_mt5_mock()  # bridge.py imports MetaTrader5 at module level

from src.bridge import CopyTradeBridge, _event_to_dict
from src.config import Config
from src.models import TradeEvent
from src.state import SharedState


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


class AccountPollRateLimitTest(unittest.TestCase):
    """Bridge polls mt5.account_info() at most once per second.

    account_info is another IPC round trip per call; equity/balance only need
    second-level freshness (the dashboard SSE frame is the consumer).
    """

    def setUp(self):
        self.mt5 = install_mt5_mock()
        self.mt5.reset_mock(return_value=True, side_effect=True)
        self.mt5.account_info.return_value = SimpleNamespace(
            balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
            leverage=100, currency="USD", login=1, server="demo", name="tester",
        )
        self.bridge = CopyTradeBridge(
            Config(), SharedState(), asyncio.Queue(), asyncio.new_event_loop()
        )

    def test_account_info_polled_once_per_second(self):
        a1 = self.bridge._get_account_info()
        a2 = self.bridge._get_account_info()
        self.assertIs(a1, a2)
        self.assertEqual(self.mt5.account_info.call_count, 1)

    def test_account_info_renews_after_interval(self):
        self.bridge._get_account_info()
        self.bridge._last_account_ts = 0.0  # simulate the 1s window passing
        self.bridge._get_account_info()
        self.assertEqual(self.mt5.account_info.call_count, 2)


class BridgeTickConnectionTest(unittest.TestCase):
    """The bridge keeps the MT5 IPC connection alive across poll cycles.

    This is the headline optimization: initialize()/shutdown() per tick is
    the dominant cost in the loop, so a tick must never tear the connection
    down.
    """

    def setUp(self):
        self.mt5 = install_mt5_mock()
        self.mt5.reset_mock(return_value=True, side_effect=True)
        self.mt5.initialize.return_value = True
        self.mt5.positions_get.return_value = []
        self.mt5.orders_get.return_value = []
        self.mt5.account_info.return_value = SimpleNamespace(
            balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
            leverage=100, currency="USD", login=1, server="demo", name="tester",
        )
        self.bridge = CopyTradeBridge(
            Config(), SharedState(), asyncio.Queue(), asyncio.new_event_loop()
        )

    def test_tick_reuses_connection_across_cycles(self):
        self.bridge._tick()
        self.bridge._tick()
        self.assertEqual(self.mt5.initialize.call_count, 1)
        self.mt5.shutdown.assert_not_called()

    def test_tick_reconnects_after_terminal_dies(self):
        self.bridge._tick()
        self.mt5.positions_get.return_value = None  # terminal went away
        self.bridge._tick()  # poll detects the dead connection
        self.bridge._tick()  # next tick re-initializes
        self.assertGreaterEqual(self.mt5.initialize.call_count, 2)


if __name__ == "__main__":
    unittest.main()
