"""Verify EA-only master mode imports without the MetaTrader5 package.

``run.py`` in EA mode (``master.ea_signals_file`` set) tails TradeSender.mq5's
signal file and never touches MT5 IPC, so the MetaTrader5 package must be
optional at import time. These tests simulate a machine where the package is
not installed and assert that the bridge/master/follower modules import
cleanly and the IPC entry points fail closed instead of raising
AttributeError.

The MetaTrader5 package IS present in this test environment (the venv), so the
package is blocked with a ``sys.meta_path`` finder inside a subprocess — this
also keeps the main test process's imported-module cache untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_NO_MT5_PROBE = r"""
import sys

class _BlockMt5:
    def find_spec(self, name, path=None, target=None):
        if name == "MetaTrader5":
            raise ImportError("MetaTrader5 blocked for this test")
        return None

sys.meta_path.insert(0, _BlockMt5())

from src import bridge, follower, master

# Import must succeed without the package (EA-only master mode).
assert bridge._MT5_AVAILABLE is False
assert master._MT5_AVAILABLE is False
assert follower._MT5_AVAILABLE is False

# Class-level ORDER_TYPE_MAP still builds with the fallback constants.
assert follower.FollowerExecutor.ORDER_TYPE_MAP[2] == 2
assert follower.FollowerExecutor.ORDER_TYPE_MAP[7] == 7

# IPC entry points fail closed instead of raising AttributeError.
from src.config import MasterConfig
m = master.MasterMonitor(MasterConfig())
assert m.connect() is False
assert m.disconnect() is None

# Follower launch fails closed with a clear message.
from src.config import FollowerConfig
f = follower.FollowerExecutor(
    FollowerConfig(name="probe", path="", port=12345, login=1, password="p", server="s")
)
assert f.launch_terminal() is False

print("no-mt5-imports-ok")
"""


class NoMt5ImportTest(unittest.TestCase):
    def test_bridge_imports_and_fails_closed_without_metatrader5(self):
        result = subprocess.run(
            [sys.executable, "-c", _NO_MT5_PROBE],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("no-mt5-imports-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
