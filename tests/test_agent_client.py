"""Tests for src.agent_client — event routing and the self-trade guard.

The self-trade guard (``_is_own_event``) is what makes a master+follower
setup safe when both run on the SAME account: the follower's own execution
is re-broadcast by the master with the follower's magic, and without the
guard it would be copied again forever.
"""

from __future__ import annotations

import unittest

from helpers import install_mt5_mock

mt5 = install_mt5_mock()  # follower.py imports MetaTrader5 at module level

from src.agent_client import AgentClient
from src.config import FollowerConfig


class OwnEventGuardTest(unittest.TestCase):
    def _client(self, magic: int = 200001, skip_own_magic: bool = True) -> AgentClient:
        cfg = FollowerConfig(
            name="t", path="C:/MT5/follower/terminal64.exe", port=15556,
            login=1, password="pw", server="srv", magic=magic,
            skip_own_magic=skip_own_magic,
        )
        c = AgentClient("http://127.0.0.1:5000", follower_cfg=cfg)
        self.addCleanup(c._mt5_pool.shutdown, wait=False)
        return c

    def test_skips_event_with_own_magic(self):
        c = self._client(magic=200001)
        self.assertTrue(c._is_own_event({"magic": 200001, "_seq_id": 5}))

    def test_passes_event_with_foreign_magic(self):
        c = self._client(magic=200001)
        self.assertFalse(c._is_own_event({"magic": 951005, "_seq_id": 5}))

    def test_passes_when_guard_disabled(self):
        c = self._client(magic=200001, skip_own_magic=False)
        self.assertFalse(c._is_own_event({"magic": 200001, "_seq_id": 5}))

    def test_passes_when_magic_missing_or_zero(self):
        c = self._client(magic=200001)
        self.assertFalse(c._is_own_event({}))
        self.assertFalse(c._is_own_event({"magic": 0, "_seq_id": 5}))

    def test_default_config_has_guard_on(self):
        cfg = FollowerConfig(
            name="t", path="C:/MT5/follower/terminal64.exe", port=15556,
            login=1, password="pw", server="srv",
        )
        self.assertTrue(cfg.skip_own_magic)


if __name__ == "__main__":
    unittest.main()
