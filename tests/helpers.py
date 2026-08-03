"""Shared helpers for the unit test suite.

The real ``MetaTrader5`` module is a Windows-only bridge that is NOT installed
in this environment. ``install_mt5_mock()`` injects a ``unittest.mock.MagicMock``
into ``sys.modules`` under that name so modules that import it at module level
(``src.master``, ``src.bridge``, ``src.follower``) can be imported and tested.
Call it BEFORE importing any ``src`` module that touches MT5.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


def install_mt5_mock() -> MagicMock:
    """Return the shared MetaTrader5 mock, creating and seeding it once.

    The ORDER_TYPE_* constants are seeded with small ints because
    ``src.follower`` reads them at class-definition time while building
    ``FollowerExecutor.ORDER_TYPE_MAP``.
    """
    mt5 = sys.modules.get("MetaTrader5")
    if mt5 is not None:
        return mt5
    mt5 = MagicMock()
    # ORDER_TYPE constants referenced at class-definition time in follower.py
    mt5.ORDER_TYPE_BUY_LIMIT = 2
    mt5.ORDER_TYPE_SELL_LIMIT = 3
    mt5.ORDER_TYPE_BUY_STOP = 4
    mt5.ORDER_TYPE_SELL_STOP = 5
    mt5.ORDER_TYPE_BUY_STOP_LIMIT = 6
    mt5.ORDER_TYPE_SELL_STOP_LIMIT = 7
    sys.modules["MetaTrader5"] = mt5
    return mt5


def fake_mt5_position(**kwargs) -> SimpleNamespace:
    """Build a fake MT5 position tuple for ``Position.from_mt5``."""
    fields = dict(
        ticket=1, symbol="XAUUSD", volume=0.5, price_open=100.0,
        price_current=100.5, profit=5.0, sl=99.0, tp=101.0,
        type=0, comment="", magic=951000,
    )
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def fake_mt5_order(**kwargs) -> SimpleNamespace:
    """Build a fake MT5 order tuple for ``PendingOrder.from_mt5``."""
    fields = dict(
        ticket=2, symbol="EURUSD", volume_current=1.0, price_open=1.1000,
        sl=0.0, tp=0.0, type=2, comment="", magic=951000, expiration=0,
    )
    fields.update(kwargs)
    return SimpleNamespace(**fields)
