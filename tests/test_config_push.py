"""Unit tests for src/config_push — hub-pushed config validation/conversion.

Pure functions only: no MT5, no network, no WS. Covers the deploy payload
schema conversion and the per-field config_update application that the
agent's WS client delegates to this module.
"""

from __future__ import annotations

import os
import unittest

from src.config import FollowerConfig
from src.config_push import (
    ApplyResult,
    apply_updates,
    build_agent_config_dict,
    build_follower_config,
    parse_bool,
    parse_deploy_config,
    parse_float,
    parse_int,
    resolve_queue_path,
)


class ParseHelpersTest(unittest.TestCase):
    def test_parse_bool_forms(self):
        self.assertTrue(parse_bool(True))
        self.assertFalse(parse_bool(False))
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("yes"))
        self.assertTrue(parse_bool("1"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("no"))
        self.assertFalse(parse_bool("0"))
        self.assertTrue(parse_bool(1))
        self.assertFalse(parse_bool(0))
        # Unknown string falls back to default
        self.assertTrue(parse_bool("garbage", True))
        self.assertFalse(parse_bool("garbage", False))

    def test_parse_int_ok(self):
        self.assertEqual(parse_int("42", "field"), 42)
        self.assertEqual(parse_int(7, "field", minv=0), 7)

    def test_parse_int_rejects(self):
        with self.assertRaises(ValueError):
            parse_int("abc", "field")
        with self.assertRaises(ValueError):
            parse_int(-1, "field", minv=0)
        with self.assertRaises(ValueError):
            parse_int(99, "field", maxv=10)

    def test_parse_float_rejects(self):
        with self.assertRaises(ValueError):
            parse_float("x", "field")
        with self.assertRaises(ValueError):
            parse_float(-0.5, "field", minv=0.0)


class ResolveQueuePathTest(unittest.TestCase):
    def test_relative_path_resolves_next_to_config(self):
        p = resolve_queue_path({}, r"C:\data\agent_config.yaml")
        self.assertEqual(p, r"C:\data\trade_queue.json")

    def test_absolute_path_kept(self):
        p = resolve_queue_path({"queue_path": r"D:\q\queue.json"}, r"C:\x\c.yaml")
        self.assertEqual(p, r"D:\q\queue.json")

    def test_explicit_relative_name(self):
        p = resolve_queue_path({"queue_path": "myq.json"}, r"C:\x\c.yaml")
        self.assertEqual(p, os.path.join(r"C:\x", "myq.json"))


class ParseDeployConfigTest(unittest.TestCase):
    def test_defaults(self):
        v = parse_deploy_config({}, r"C:\d\agent_config.yaml")
        self.assertEqual(v.port, 0)
        self.assertEqual(v.login, 0)
        self.assertEqual(v.lot_multiplier, 1.0)
        self.assertEqual(v.max_lot, 10.0)
        self.assertEqual(v.min_lot, 0.01)
        self.assertEqual(v.max_positions, 10)
        self.assertEqual(v.deviation, 50)
        self.assertEqual(v.magic, 951001)
        self.assertEqual(v.symbol_mapping, {})
        self.assertEqual(v.queue_path, r"C:\d\trade_queue.json")

    def test_values_and_mapping(self):
        cfg = {
            "port": 1122, "login": 123456, "lot_multiplier": 2.0,
            "max_lot": 5.0, "symbol_mapping": {"xauusd": "XAUUSDc"},
            "queue_path": r"D:\q.json",
        }
        v = parse_deploy_config(cfg, r"C:\d\agent_config.yaml")
        self.assertEqual(v.port, 1122)
        self.assertEqual(v.login, 123456)
        self.assertEqual(v.lot_multiplier, 2.0)
        self.assertEqual(v.symbol_mapping, {"XAUUSD": "XAUUSDc"})
        self.assertEqual(v.queue_path, r"D:\q.json")

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            parse_deploy_config({"port": "high"}, r"C:\d\a.yaml")

    def test_non_dict_mapping_ignored(self):
        v = parse_deploy_config({"symbol_mapping": "nope"}, r"C:\d\a.yaml")
        self.assertEqual(v.symbol_mapping, {})


class BuildAgentConfigDictTest(unittest.TestCase):
    def test_schema_conversion(self):
        cfg = {"path": r"C:\MT5\terminal64.exe", "server": "Exness-MT5Trial7",
               "skip_own_magic": False, "dry_run": True}
        v = parse_deploy_config({"login": 433903489}, r"C:\d\agent_config.yaml")
        out = build_agent_config_dict(
            cfg, v, agent_name="f1", hub_url="ws://hub:5000", data_dir=r"C:\d",
        )
        self.assertEqual(out["name"], "f1")
        self.assertEqual(out["hub_url"], "ws://hub:5000")
        self.assertEqual(out["mt5_login"], 433903489)
        self.assertEqual(out["mt5_path"], r"C:\MT5\terminal64.exe")
        self.assertEqual(out["mt5_server"], "Exness-MT5Trial7")
        self.assertFalse(out["skip_own_magic"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["log_file"], os.path.join(r"C:\d", "logs", "agent.log"))
        self.assertEqual(out["log_level"], "INFO")


class BuildFollowerConfigTest(unittest.TestCase):
    def test_max_lot_default_quirk(self):
        # Historical behaviour: deploy YAML schema defaults max_lot=10.0 but
        # the live FollowerConfig defaults it to 1.0. Preserve both.
        v = parse_deploy_config({}, r"C:\d\agent_config.yaml")
        self.assertEqual(v.max_lot, 10.0)
        cfg = build_follower_config({}, v, agent_name="f1")
        self.assertIsInstance(cfg, FollowerConfig)
        self.assertEqual(cfg.max_lot, 1.0)
        self.assertEqual(cfg.name, "f1")
        self.assertEqual(cfg.queue_path, r"C:\d\trade_queue.json")

    def test_explicit_values_flow_through(self):
        payload = {
            "name": "f2", "login": 99, "password": "pw", "server": "srv",
            "lot_multiplier": 3.0, "max_lot": 2.0,
            "terminal_data_path": r"C:\data\files",
        }
        v = parse_deploy_config(payload, r"C:\d\agent_config.yaml")
        cfg = build_follower_config(payload, v, agent_name="fallback")
        self.assertEqual(cfg.name, "f2")
        self.assertEqual(cfg.login, 99)
        self.assertEqual(cfg.password, "pw")
        self.assertEqual(cfg.server, "srv")
        self.assertEqual(cfg.lot_multiplier, 3.0)
        self.assertEqual(cfg.max_lot, 2.0)
        self.assertEqual(cfg.terminal_data_path, r"C:\data\files")


class ApplyUpdatesTest(unittest.TestCase):
    def _cfg(self) -> FollowerConfig:
        return FollowerConfig(
            name="f", path=r"C:\MT5\terminal64.exe", port=0, login=0,
            password="", server="", lot_multiplier=1.0, max_lot=10.0,
            min_lot=0.01, max_positions=10, deviation=50, magic=951001,
            skip_own_magic=True, symbol_mapping={}, skip_auto_trading=True,
            terminal_data_path="", max_daily_loss=0.0,
            max_drawdown_pct=0.0, max_daily_trades=0,
            queue_path=r"C:\q.json",
        )

    def test_empty_config_noop(self):
        r = apply_updates(self._cfg(), {})
        self.assertEqual(r.changed, [])
        self.assertEqual(r.errors, [])
        self.assertIsNone(r.queue_path)

    def test_valid_updates(self):
        cfg = self._cfg()
        r = apply_updates(cfg, {
            "lot_multiplier": 2.0, "max_positions": 5, "magic": 777,
        })
        self.assertEqual(cfg.lot_multiplier, 2.0)
        self.assertEqual(cfg.max_positions, 5)
        self.assertEqual(cfg.magic, 777)
        self.assertEqual(len(r.changed), 3)
        self.assertEqual(r.errors, [])

    def test_invalid_field_rejected_others_apply(self):
        cfg = self._cfg()
        r = apply_updates(cfg, {"lot_multiplier": -1.0, "deviation": 100})
        self.assertEqual(cfg.lot_multiplier, 1.0)  # untouched
        self.assertEqual(cfg.deviation, 100)       # applied
        self.assertEqual(len(r.errors), 1)
        self.assertIn("lot_multiplier", r.errors[0])

    def test_queue_path_valid(self):
        cfg = self._cfg()
        r = apply_updates(cfg, {"queue_path": r"D:\q2.json"})
        self.assertEqual(cfg.queue_path, r"D:\q2.json")
        self.assertEqual(r.queue_path, r"D:\q2.json")

    def test_queue_path_invalid(self):
        cfg = self._cfg()
        r = apply_updates(cfg, {"queue_path": ""})
        self.assertEqual(cfg.queue_path, r"C:\q.json")
        self.assertEqual(len(r.errors), 1)

    def test_symbol_mapping_uppercased(self):
        cfg = self._cfg()
        r = apply_updates(cfg, {"symbol_mapping": {"btcusd": "BTCUSDm"}})
        self.assertEqual(cfg.symbol_mapping, {"BTCUSD": "BTCUSDm"})
        self.assertFalse(r.errors)

    def test_symbol_mapping_non_dict(self):
        cfg = self._cfg()
        r = apply_updates(cfg, {"symbol_mapping": "x"})
        self.assertEqual(cfg.symbol_mapping, {})
        self.assertEqual(len(r.errors), 1)


if __name__ == "__main__":
    unittest.main()
