"""Tests for src.follower trade-execution helpers."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace

from helpers import install_mt5_mock

mt5 = install_mt5_mock()  # follower.py imports MetaTrader5 at module level

# Keep expected-warning test paths (corrupt queue file, blocked risk limits)
# from spamming stderr via logging's lastResort handler.
_log = logging.getLogger("src.follower")
_log.addHandler(logging.NullHandler())
_log.propagate = False

from src.config import FollowerConfig
from src.follower import FollowerExecutor
from src.models import TradeEvent


def _make_config(td: str, **over) -> FollowerConfig:
    base = dict(name="f1", path="C:/MT5/follower/terminal64.exe", port=15556,
                login=1, password="pw", server="srv",
                queue_path=os.path.join(td, "queue.json"))
    base.update(over)
    return FollowerConfig(**base)


class OrderTypeMapTest(unittest.TestCase):
    def test_map_contains_all_pending_order_types(self):
        self.assertEqual(FollowerExecutor.ORDER_TYPE_MAP, {
            2: mt5.ORDER_TYPE_BUY_LIMIT,
            3: mt5.ORDER_TYPE_SELL_LIMIT,
            4: mt5.ORDER_TYPE_BUY_STOP,
            5: mt5.ORDER_TYPE_SELL_STOP,
            6: mt5.ORDER_TYPE_BUY_STOP_LIMIT,
            7: mt5.ORDER_TYPE_SELL_STOP_LIMIT,
        })


class ApplyLotScalingTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        mt5.symbol_info.return_value = SimpleNamespace(volume_step=0.1, digits=2)

    def test_multiplier_and_step_rounding(self):
        ex = FollowerExecutor(_make_config(self._td.name, lot_multiplier=2.0))
        self.assertEqual(ex._apply_lot_scaling(0.5, "XAUUSD"), 1.0)
        # 0.04 * 2 = 0.08 -> rounds UP to the 0.1 step
        self.assertEqual(ex._apply_lot_scaling(0.04, "XAUUSD"), 0.1)
        # 1.2345 * 2 = 2.469 -> nearest 0.1 step = 2.5
        self.assertEqual(ex._apply_lot_scaling(1.2345, "XAUUSD"), 2.5)

    def test_min_max_clamps(self):
        ex = FollowerExecutor(_make_config(
            self._td.name, lot_multiplier=2.0, min_lot=0.01, max_lot=5.0))
        # above max -> clamped to max_lot
        self.assertEqual(ex._apply_lot_scaling(3.5, "XAUUSD"), 5.0)
        # below min -> clamped to min_lot
        self.assertEqual(ex._apply_lot_scaling(0.001, "XAUUSD"), 0.01)

    def test_no_symbol_falls_back_to_two_decimals(self):
        ex = FollowerExecutor(_make_config(self._td.name, lot_multiplier=1.0))
        self.assertEqual(ex._apply_lot_scaling(0.333), 0.33)

    def test_identity_multiplier(self):
        ex = FollowerExecutor(_make_config(self._td.name, lot_multiplier=1.0))
        self.assertEqual(ex._apply_lot_scaling(0.5, "XAUUSD"), 0.5)


class MapPriceTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        mt5.symbol_info.side_effect = (
            lambda s: SimpleNamespace(volume_step=0.1, digits=2)
            if s == "EURUSD" else None
        )

    def test_uses_symbol_digits(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        self.assertEqual(ex._map_price(1.234567, "EURUSD"), 1.23)
        self.assertEqual(ex._map_price(125.34567, "EURUSD"), 125.35)

    def test_jpy_fallback_price_ge_100_three_decimals(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        self.assertEqual(ex._map_price(125.34567, "UNKNOWN"), 125.346)
        self.assertEqual(ex._map_price(125.34567), 125.346)

    def test_default_fallback_five_decimals(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        self.assertEqual(ex._map_price(1.234567, "UNKNOWN"), 1.23457)
        self.assertEqual(ex._map_price(1.234567), 1.23457)


class LoadQueueTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def test_missing_file_returns_empty(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        self.assertEqual(ex._load_queue(), [])

    def test_valid_list_loaded(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        data = [{"event": {"action": "open"}, "timestamp": 1.0}]
        with open(ex._cfg.queue_path, "w") as f:
            json.dump(data, f)
        self.assertEqual(ex._load_queue(), data)

    def test_non_list_json_resets_to_empty(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        with open(ex._cfg.queue_path, "w") as f:
            json.dump({"oops": True}, f)
        self.assertEqual(ex._load_queue(), [])

    def test_corrupt_json_resets_to_empty(self):
        ex = FollowerExecutor(_make_config(self._td.name))
        with open(ex._cfg.queue_path, "w") as f:
            f.write("{ not json !")
        self.assertEqual(ex._load_queue(), [])


class CheckRiskLimitsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        mt5.initialize.return_value = True
        mt5.login.return_value = True
        mt5.shutdown.return_value = None
        self.acc = SimpleNamespace(login=1, server="srv", equity=1000.0,
                                   balance=1000.0)
        mt5.account_info.return_value = self.acc
        mt5.terminal_info.return_value = SimpleNamespace(trade_allowed=True)
        mt5.history_deals_get.return_value = []

    def _ex(self, **over) -> FollowerExecutor:
        return FollowerExecutor(_make_config(
            self._td.name, skip_auto_trading=True, **over))

    @staticmethod
    def _deal(profit: float, position_id: int) -> SimpleNamespace:
        return SimpleNamespace(profit=profit, position_id=position_id)

    def test_max_daily_trades_counts_distinct_positions(self):
        ex = self._ex(max_daily_trades=2)
        mt5.history_deals_get.return_value = [
            self._deal(-10.0, 1),
            self._deal(-5.0, 1),   # same position: counted once
            self._deal(0.0, 0),    # deposit: not a trade
            self._deal(2.0, 0),
        ]
        self.assertTrue(ex._check_risk_limits())   # 1 distinct < 2
        mt5.history_deals_get.return_value.append(self._deal(1.0, 7))
        self.assertFalse(ex._check_risk_limits())  # 2 distinct >= 2

    def test_max_daily_loss_sums_profits(self):
        ex = self._ex(max_daily_loss=100.0)
        mt5.history_deals_get.return_value = [
            self._deal(-50.0, 1), self._deal(-40.0, 2), self._deal(-20.0, 3),
        ]
        self.assertFalse(ex._check_risk_limits())  # -110 <= -100 blocks
        mt5.history_deals_get.return_value = [
            self._deal(-50.0, 1), self._deal(-40.0, 2),
        ]
        self.assertTrue(ex._check_risk_limits())   # -90 > -100 ok
        # positive PnL never triggers the loss guard
        mt5.history_deals_get.return_value = [
            self._deal(50.0, 1), self._deal(-20.0, 2),
        ]
        self.assertTrue(ex._check_risk_limits())

    def test_peak_equity_drawdown(self):
        ex = self._ex(max_drawdown_pct=10.0)
        self.acc.equity = 1000.0
        self.assertTrue(ex._check_risk_limits())   # peak=1000, dd 0%
        self.assertEqual(ex._peak_equity, 1000.0)
        self.acc.equity = 850.0
        self.assertFalse(ex._check_risk_limits())  # dd 15% >= 10% blocks
        self.acc.equity = 900.0
        self.assertFalse(ex._check_risk_limits())  # dd exactly 10% blocks
        self.acc.equity = 1100.0
        self.assertTrue(ex._check_risk_limits())   # new peak, dd 0%

    def test_history_none_allows_trade(self):
        ex = self._ex(max_daily_loss=100.0, max_daily_trades=5)
        mt5.history_deals_get.return_value = None
        self.assertTrue(ex._check_risk_limits())

    def test_file_based_mode_skips_checks(self):
        ex = FollowerExecutor(_make_config(
            self._td.name,
            terminal_data_path=os.path.join(self._td.name, "files"),
        ))
        mt5.initialize.reset_mock()
        mt5.history_deals_get.reset_mock()
        self.assertTrue(ex.is_file_based())
        self.assertTrue(ex._check_risk_limits())
        mt5.initialize.assert_not_called()
        mt5.history_deals_get.assert_not_called()


class FileRelayCommandTest(unittest.TestCase):
    """Command serialization + result handling for the file-relay follower."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ex = FollowerExecutor(_make_config(
            self._td.name,
            terminal_data_path=os.path.join(self._td.name, "files"),
            skip_auto_trading=True,
        ))
        # No 'c'-suffix auto-mapping, no volume step: symbol stays BTCUSD and
        # volumes round to 2 decimals, so command strings are predictable.
        mt5.symbol_info.side_effect = lambda s: None

    def _event(self, action: str, **over) -> TradeEvent:
        base = dict(
            action=action, symbol="BTCUSD", volume=0.01, price=65000.0,
            sl=64500.0, tp=66000.0, master_ticket=50, position_type=2,
            comment="c", magic=0, order_type=2, expiration=1800000000,
        )
        base.update(over)
        return TradeEvent(**base)

    # ── Command serialization ────────────────────────────────────────

    def test_open_command_format(self):
        e = self._event("open", position_type=0, order_type=None)
        self.assertEqual(
            self.ex._file_build_command("OPEN_BUY", e),
            "OPEN_BUY|BTCUSD|0.01|64500.00000|66000.00000|50",
        )

    def test_place_command_format(self):
        self.assertEqual(
            self.ex._file_build_command("PLACE_ORDER", self._event("place")),
            "PLACE_ORDER|BTCUSD|2|0.01|65000.00000|64500.00000|66000.00000|1800000000|50",
        )

    def test_place_command_no_expiration(self):
        e = self._event("place", expiration=None)
        self.assertEqual(
            self.ex._file_build_command("PLACE_ORDER", e),
            "PLACE_ORDER|BTCUSD|2|0.01|65000.00000|64500.00000|66000.00000|0|50",
        )

    def test_modify_order_command_format(self):
        e = self._event("modify_order", price=64800.0)
        self.assertEqual(
            self.ex._file_build_command("MODIFY_ORDER", e),
            "MODIFY_ORDER|BTCUSD|2|0.01|64800.00000|64500.00000|66000.00000|1800000000|50",
        )

    def test_delete_order_command_format(self):
        self.assertEqual(
            self.ex._file_build_command("DELETE_ORDER", self._event("delete")),
            "DELETE_ORDER|50",
        )

    def test_close_command_format(self):
        # TradeReceiver.mq5 parses CLOSE's ticket from p[1] (`CLOSE|<ticket>`),
        # unlike market orders where the ticket sits in p[5]. Sending the
        # 6-field market layout would make the EA read the symbol as the ticket
        # and never close the real position.
        self.assertEqual(
            self.ex._file_build_command("CLOSE", self._event("close")),
            "CLOSE|50",
        )

    # ── Execution + result handling ──────────────────────────────────

    def test_place_executes_to_done(self):
        self.ex._file_send_command = lambda a, e: "DONE|123"
        self.assertTrue(self.ex._file_execute_event(self._event("place")))

    def test_close_not_found_is_benign(self):
        # Bridge missed the open (was down) and now only the CLOSE arrives:
        # the follower has nothing to close — already consistent, not an error.
        self.ex._file_send_command = lambda a, e: "FAILED|NF_COMMENT"
        self.assertTrue(self.ex._file_execute_event(self._event("close")))

    def test_delete_not_found_is_benign(self):
        self.ex._file_send_command = lambda a, e: "FAILED|NF"
        self.assertTrue(self.ex._file_execute_event(self._event("delete")))

    def test_modify_not_found_is_benign(self):
        self.ex._file_send_command = lambda a, e: "FAILED|NF_COMMENT"
        self.assertTrue(self.ex._file_execute_event(self._event("modify")))

    def test_open_failure_is_not_benign(self):
        self.ex._file_send_command = lambda a, e: "FAILED|PRICE"
        self.assertFalse(self.ex._file_execute_event(self._event("open")))

    def test_place_failure_is_not_benign(self):
        self.ex._file_send_command = lambda a, e: "FAILED|INVALID_PRICE"
        self.assertFalse(self.ex._file_execute_event(self._event("place")))


class HistoryDedupTest(unittest.TestCase):
    """Stale re-delivery protection: OPEN/PLACE events whose round trip already
    completed (visible in deal/order history) must be silent no-ops, and
    CLOSE/MODIFY/DELETE of already-done tickets must not error out."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        mt5.initialize.return_value = True
        mt5.login.return_value = True
        mt5.shutdown.return_value = None
        mt5.account_info.return_value = SimpleNamespace(
            login=1, server="srv", equity=1000.0, balance=1000.0,
        )
        mt5.terminal_info.return_value = SimpleNamespace(trade_allowed=True)
        mt5.positions_get.return_value = []
        mt5.orders_get.return_value = []
        mt5.history_deals_get.return_value = []
        mt5.history_orders_get.return_value = []
        # The module-level mt5 mock is shared across all test classes — reset
        # call history so assert_not_called() in the skip tests is meaningful.
        mt5.order_send.reset_mock()
        mt5.positions_get.reset_mock()
        mt5.orders_get.reset_mock()
        mt5.history_deals_get.reset_mock()
        mt5.history_orders_get.reset_mock()
        mt5.symbol_info.side_effect = lambda s: None
        mt5.symbol_info_tick.return_value = SimpleNamespace(ask=1.2, bid=1.2)
        mt5.order_send.return_value = SimpleNamespace(
            retcode=mt5.TRADE_RETCODE_DONE, order=111,
        )
        self.ex = FollowerExecutor(_make_config(
            self._td.name, skip_auto_trading=True, magic=951003,
        ))

    def _event(self, action: str, **over) -> TradeEvent:
        base = dict(
            action=action, symbol="BTCUSD", volume=0.01, price=0.0,
            sl=None, tp=None, master_ticket=12345, position_type=0,
            comment="", magic=0, prev_volume=None, order_type=None,
            expiration=None,
        )
        base.update(over)
        return TradeEvent(**base)

    @staticmethod
    def _deal(comment: str, magic: int) -> SimpleNamespace:
        return SimpleNamespace(comment=comment, magic=magic,
                               profit=0.0, position_id=1)

    def test_open_skipped_when_round_trip_in_history(self):
        # Ticket was opened AND closed earlier (both deals in history), but no
        # position is currently open. A stale replay must NOT re-open it.
        mt5.history_deals_get.return_value = [self._deal("12345", 951003)]
        self.assertTrue(self.ex.execute(self._event("open")))
        mt5.order_send.assert_not_called()
        self.assertEqual(self.ex._load_queue(), [])

    def test_open_skipped_when_position_currently_open(self):
        mt5.positions_get.return_value = [
            SimpleNamespace(comment="12345", magic=951003, ticket=9,
                            sl=0.0, tp=0.0),
        ]
        self.assertTrue(self.ex.execute(self._event("open")))
        mt5.order_send.assert_not_called()
        self.assertEqual(self.ex._load_queue(), [])

    def test_open_executes_when_never_materialized(self):
        self.assertTrue(self.ex.execute(self._event("open")))
        mt5.order_send.assert_called_once()

    def test_place_skipped_when_order_in_history(self):
        mt5.history_orders_get.return_value = [self._deal("12345", 951003)]
        self.assertTrue(self.ex.execute(self._event("place", order_type=2)))
        mt5.order_send.assert_not_called()
        self.assertEqual(self.ex._load_queue(), [])

    def test_close_stale_after_round_trip_is_silent_success(self):
        mt5.history_deals_get.return_value = [self._deal("12345", 951003)]
        self.assertTrue(self.ex.execute(self._event("close")))
        mt5.order_send.assert_not_called()

    def test_close_missing_without_history_fails(self):
        self.assertFalse(self.ex.execute(self._event("close")))
        mt5.order_send.assert_not_called()

    def test_modify_stale_after_round_trip_is_silent_success(self):
        mt5.history_deals_get.return_value = [self._deal("12345", 951003)]
        self.assertTrue(self.ex.execute(self._event("modify", sl=1.0)))

    def test_stale_open_in_queue_is_dropped_on_replay(self):
        entry = {
            "event": {
                "action": "open", "symbol": "BTCUSD", "volume": 0.01,
                "price": 0.0, "sl": None, "tp": None, "master_ticket": 12345,
                "position_type": 0, "comment": "", "magic": 0,
                "prev_volume": None, "order_type": None, "expiration": None,
            },
            "timestamp": 1.0, "retry_count": 0,
        }
        with open(self.ex._cfg.queue_path, "w") as f:
            json.dump([entry], f)
        mt5.history_deals_get.return_value = [self._deal("12345", 951003)]
        self.ex._dequeue_and_replay()
        mt5.order_send.assert_not_called()
        self.assertEqual(self.ex._load_queue(), [])

    def test_queue_save_is_atomic(self):
        data = [{"event": {"action": "open"}, "timestamp": 1.0}]
        self.ex._save_queue(data)
        self.assertEqual(self.ex._load_queue(), data)
        self.assertFalse(os.path.exists(self.ex._cfg.queue_path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
