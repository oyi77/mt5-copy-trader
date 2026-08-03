"""Tests for src.config (loading, validation, serialization, rollback)."""

from __future__ import annotations

import os
import tempfile
import unittest

from src.config import (
    Config,
    agent_config_to_yaml,
    follower_to_safe_dict,
    load_agent_config,
    load_config,
    save_config,
)

VALID_YAML = """\
master:
  path: "C:/MT5/master/terminal64.exe"
  port: 15555
followers:
  - name: f1
    path: "C:/MT5/follower1/terminal64.exe"
    port: 15556
    login: 12345
    password: supersecret
    server: DemoServer
    lot_multiplier: 1.5
    max_lot: 3.0
    min_lot: 0.05
server:
  host: 127.0.0.1
  port: 5000
poll_interval_ms: 300
logging:
  level: DEBUG
  file: logs/foo.log
  max_size_mb: 10
  backup_count: 5
"""


class LoadConfigTest(unittest.TestCase):
    def _write(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        self.addCleanup(os.remove, path)
        return path

    def test_valid_config_round_trips(self):
        cfg = load_config(self._write(VALID_YAML))
        self.assertEqual(cfg.master.path, "C:/MT5/master/terminal64.exe")
        self.assertEqual(cfg.master.port, 15555)
        self.assertEqual(len(cfg.followers), 1)
        f = cfg.followers[0]
        self.assertEqual(f.name, "f1")
        self.assertEqual(f.path, "C:/MT5/follower1/terminal64.exe")
        self.assertEqual(f.port, 15556)
        self.assertEqual(f.login, 12345)
        self.assertEqual(f.password, "supersecret")
        self.assertEqual(f.server, "DemoServer")
        self.assertEqual(f.lot_multiplier, 1.5)
        self.assertEqual(f.max_lot, 3.0)
        self.assertEqual(f.min_lot, 0.05)
        self.assertEqual(cfg.poll_interval_ms, 300)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 5000)
        self.assertEqual(cfg.logging.level, "DEBUG")
        self.assertEqual(cfg.logging.file, "logs/foo.log")
        self.assertEqual(cfg.logging.max_size_mb, 10)
        self.assertEqual(cfg.logging.backup_count, 5)

        # Save + reload must preserve values
        out = self._write("")
        save_config(cfg, out)
        cfg2 = load_config(out)
        self.assertEqual(cfg2.master.path, "C:/MT5/master/terminal64.exe")
        self.assertEqual(cfg2.master.port, 15555)
        self.assertEqual(len(cfg2.followers), 1)
        f2 = cfg2.followers[0]
        self.assertEqual(f2.name, "f1")
        self.assertEqual(f2.port, 15556)
        self.assertEqual(f2.login, 12345)
        self.assertEqual(f2.password, "supersecret")
        self.assertEqual(f2.lot_multiplier, 1.5)
        self.assertEqual(cfg2.poll_interval_ms, 300)
        self.assertEqual(cfg2.port, 5000)

    def test_missing_file_raises_file_not_found(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        os.remove(path)
        with self.assertRaises(FileNotFoundError):
            load_config(path)

    def test_ea_signals_file_round_trip(self):
        yaml_text = """\
master:
  path: C:/x
  port: 15555
  ea_signals_file: "C:/Users/me/mt5_exness/MQL5/Files/master_signals.txt"
"""
        cfg = load_config(self._write(yaml_text))
        self.assertEqual(
            cfg.master.ea_signals_file,
            "C:/Users/me/mt5_exness/MQL5/Files/master_signals.txt",
        )
        # Save + reload must preserve it
        out = self._write("")
        save_config(cfg, out)
        cfg2 = load_config(out)
        self.assertEqual(
            cfg2.master.ea_signals_file,
            "C:/Users/me/mt5_exness/MQL5/Files/master_signals.txt",
        )

    def test_ea_signals_file_defaults_empty(self):
        cfg = load_config(self._write("master:\n  path: C:/x\n  port: 15555\n"))
        self.assertEqual(cfg.master.ea_signals_file, "")

    def test_ea_watchdog_fields_round_trip(self):
        yaml_text = """\
master:
  path: C:/x
  port: 15555
  ea_signals_file: "C:/sigs.txt"
  ea_watchdog: true
  ea_watchdog_attach_script: "C:/attach_ea.ps1"
  login: 433903489
  password: "secret"
  server: "Exness-MT5Trial7"
"""
        cfg = load_config(self._write(yaml_text))
        self.assertTrue(cfg.master.ea_watchdog)
        self.assertEqual(cfg.master.ea_watchdog_attach_script, "C:/attach_ea.ps1")
        self.assertEqual(cfg.master.login, 433903489)
        self.assertEqual(cfg.master.password, "secret")
        self.assertEqual(cfg.master.server, "Exness-MT5Trial7")
        # Save + reload must preserve them
        out = self._write("")
        save_config(cfg, out)
        cfg2 = load_config(out)
        self.assertTrue(cfg2.master.ea_watchdog)
        self.assertEqual(cfg2.master.ea_watchdog_attach_script, "C:/attach_ea.ps1")
        self.assertEqual(cfg2.master.login, 433903489)
        self.assertEqual(cfg2.master.password, "secret")
        self.assertEqual(cfg2.master.server, "Exness-MT5Trial7")

    def test_ea_watchdog_fields_default_off(self):
        cfg = load_config(self._write("master:\n  path: C:/x\n  port: 15555\n"))
        self.assertFalse(cfg.master.ea_watchdog)
        self.assertEqual(cfg.master.ea_watchdog_attach_script, "")
        self.assertEqual(cfg.master.login, 0)
        self.assertEqual(cfg.master.password, "")
        self.assertEqual(cfg.master.server, "")

    def test_master_port_zero_invalid(self):
        with self.assertRaises(ValueError):
            load_config(self._write("master:\n  path: C:/x\n  port: 0\n"))

    def test_master_port_above_range_invalid(self):
        with self.assertRaises(ValueError):
            load_config(self._write("master:\n  path: C:/x\n  port: 70000\n"))

    def test_follower_port_zero_invalid(self):
        yaml_text = """\
master:
  path: C:/x
  port: 15555
followers:
  - {name: f1, port: 0, login: 1, password: p, server: s}
"""
        with self.assertRaises(ValueError):
            load_config(self._write(yaml_text))

    def test_duplicate_follower_names_invalid(self):
        yaml_text = """\
master:
  path: C:/x
  port: 15555
followers:
  - {name: dup, port: 15556, login: 1, password: p, server: s}
  - {name: dup, port: 15557, login: 2, password: p, server: s}
"""
        with self.assertRaises(ValueError):
            load_config(self._write(yaml_text))

    def test_min_lot_gt_max_lot_invalid(self):
        yaml_text = """\
master:
  path: C:/x
  port: 15555
followers:
  - {name: f1, port: 15556, login: 1, password: p, server: s, min_lot: 5.0, max_lot: 1.0}
"""
        with self.assertRaises(ValueError):
            load_config(self._write(yaml_text))

    def test_poll_interval_below_minimum_invalid(self):
        yaml_text = """\
master:
  path: C:/x
  port: 15555
poll_interval_ms: 50
"""
        with self.assertRaises(ValueError):
            load_config(self._write(yaml_text))

    def test_skip_own_magic_defaults_on_and_round_trips(self):
        # YAML without the key → default True (loop-safe by default)
        cfg = load_config(self._write(VALID_YAML))
        self.assertTrue(cfg.followers[0].skip_own_magic)
        self.assertEqual(load_agent_config(self._write(
            "hub_url: http://h:5000\nmt5_login: 1\nmt5_password: p\nmt5_server: s\n"
        )).skip_own_magic, True)

        # Explicit false is parsed and survives save+reload
        cfg.followers[0].skip_own_magic = False
        out = self._write("")
        save_config(cfg, out)
        self.assertFalse(load_config(out).followers[0].skip_own_magic)

        # Agent config path: false parsed from YAML
        ac = load_agent_config(self._write(
            "hub_url: http://h:5000\nmt5_login: 1\nmt5_password: p\nmt5_server: s\n"
            "magic: 200001\nskip_own_magic: false\n"
        ))
        self.assertFalse(ac.skip_own_magic)
        self.assertEqual(ac.magic, 200001)

    def test_add_follower_parses_skip_own_magic(self):
        cfg = Config()
        f = cfg.add_follower({
            "name": "f1", "path": "C:/x.exe", "port": 15556,
            "login": 1, "password": "p", "server": "s",
            "magic": 7, "skip_own_magic": False,
        })
        self.assertFalse(f.skip_own_magic)
        self.assertEqual(f.magic, 7)
        # Default stays on when omitted
        f2 = cfg.add_follower({
            "name": "f2", "path": "C:/x.exe", "port": 15557,
            "login": 2, "password": "p", "server": "s",
        })
        self.assertTrue(f2.skip_own_magic)

    def test_nonpositive_lot_multiplier_invalid(self):
        for bad in ("0", "-1"):
            yaml_text = f"""\
master:
  path: C:/x
  port: 15555
followers:
  - {{name: f1, port: 15556, login: 1, password: p, server: s, lot_multiplier: {bad}}}
"""
            with self.assertRaises(ValueError):
                load_config(self._write(yaml_text))


class FollowerSafeDictTest(unittest.TestCase):
    def _follower(self, password: str = "hunter2"):
        cfg = Config()
        return cfg.add_follower({
            "name": "a", "port": 15556, "login": 1,
            "password": password, "server": "s",
        })

    def test_never_contains_password(self):
        d = follower_to_safe_dict(self._follower())
        self.assertNotIn("password", d)
        self.assertTrue(d["has_password"])
        self.assertEqual(d["name"], "a")
        self.assertEqual(d["login"], 1)
        self.assertEqual(d["port"], 15556)

    def test_has_password_false_when_empty(self):
        d = follower_to_safe_dict(self._follower(password=""))
        self.assertFalse(d["has_password"])


class AgentConfigYamlTest(unittest.TestCase):
    def _config(self) -> Config:
        cfg = Config()
        cfg.add_follower({
            "name": "f1", "path": "C:/MT5/x.exe", "port": 15556,
            "login": 42, "password": "pw", "server": "Srv",
            "lot_multiplier": 2.0, "min_lot": 0.1, "max_lot": 4.0,
            "max_positions": 10, "deviation": 30, "magic": 999,
            "symbol_mapping": {"XAUUSD": "XAUUSDc"},
        })
        return cfg

    def test_contains_expected_fields(self):
        y = agent_config_to_yaml(self._config(), "f1", "http://hub:5000")
        for needle in (
            'name: "f1"',
            'hub_url: "http://hub:5000"',
            'mt5_path: "C:/MT5/x.exe"',
            "mt5_port: 15556",
            "mt5_login: 42",
            'mt5_password: "pw"',
            'mt5_server: "Srv"',
            "lot_multiplier: 2.0",
            "max_lot: 4.0",
            "min_lot: 0.1",
            "max_positions: 10",
            "deviation: 30",
            "magic: 999",
            "skip_own_magic: true",
            '"XAUUSD": "XAUUSDc"',
            "max_daily_loss:",
            "max_drawdown_pct:",
            "max_daily_trades:",
            "queue_path:",
            "terminal_data_path:",
            "log_file:",
            "log_level:",
        ):
            self.assertIn(needle, y, f"missing {needle!r} in generated YAML")

    def test_unknown_follower_raises(self):
        with self.assertRaises(ValueError):
            agent_config_to_yaml(Config(), "nope", "http://hub:5000")

    def test_generated_yaml_reloads_as_agent_config(self):
        y = agent_config_to_yaml(self._config(), "f1", "http://hub:5000")
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(y)
        self.addCleanup(os.remove, path)
        ac = load_agent_config(path)
        self.assertEqual(ac.name, "f1")
        self.assertEqual(ac.hub_url, "http://hub:5000")
        self.assertEqual(ac.mt5_port, 15556)
        self.assertEqual(ac.mt5_login, 42)
        self.assertEqual(ac.mt5_password, "pw")
        self.assertEqual(ac.mt5_server, "Srv")
        self.assertEqual(ac.lot_multiplier, 2.0)


class AddUpdateFollowerRollbackTest(unittest.TestCase):
    def test_add_follower_rolls_back_on_value_error(self):
        cfg = Config()
        cfg.add_follower({"name": "a", "port": 15556, "login": 1, "server": "s"})
        self.assertEqual(len(cfg.followers), 1)

        with self.assertRaises(ValueError):
            cfg.add_follower({"name": "b", "min_lot": 5, "max_lot": 1})
        self.assertEqual(len(cfg.followers), 1)

        with self.assertRaises(ValueError):
            cfg.add_follower({"name": "a", "port": 15557, "login": 2})
        self.assertEqual(len(cfg.followers), 1)

        with self.assertRaises(ValueError):
            cfg.add_follower({"name": "c", "port": 0, "login": 1})
        self.assertEqual(len(cfg.followers), 1)

    def test_update_follower_rolls_back_on_value_error(self):
        cfg = Config()
        cfg.add_follower({
            "name": "a", "port": 15556, "login": 1, "server": "s",
            "min_lot": 0.01, "max_lot": 5.0,
        })
        with self.assertRaises(ValueError):
            cfg.update_follower("a", {"port": 0})
        self.assertEqual(cfg.followers[0].port, 15556)

        with self.assertRaises(ValueError):
            cfg.update_follower("a", {"min_lot": 5.0, "max_lot": 1.0})
        self.assertEqual(cfg.followers[0].min_lot, 0.01)
        self.assertEqual(cfg.followers[0].max_lot, 5.0)

        with self.assertRaises(ValueError):
            cfg.update_follower("a", {"lot_multiplier": 0})
        self.assertEqual(cfg.followers[0].lot_multiplier, 1.0)

    def test_update_follower_duplicate_name_rolls_back(self):
        cfg = Config()
        cfg.add_follower({"name": "a", "port": 15556, "login": 1, "server": "s"})
        cfg.add_follower({"name": "b", "port": 15557, "login": 2, "server": "s"})
        with self.assertRaises(ValueError):
            cfg.update_follower("b", {"name": "a"})
        self.assertEqual(cfg.followers[1].name, "b")

    def test_update_follower_success_path(self):
        cfg = Config()
        cfg.add_follower({"name": "a", "port": 15556, "login": 1, "server": "s"})
        f = cfg.update_follower("a", {"port": 20000, "max_lot": 9.0})
        self.assertIsNotNone(f)
        self.assertEqual(f.port, 20000)
        self.assertEqual(f.max_lot, 9.0)

    def test_update_follower_unknown_name_returns_none(self):
        cfg = Config()
        self.assertIsNone(cfg.update_follower("zzz", {"port": 1}))


if __name__ == "__main__":
    unittest.main()
