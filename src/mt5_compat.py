"""Shared import guard for the optional MetaTrader5 package.

``MetaTrader5`` is a Windows-only bridge that may be absent on machines running
EA-only master mode (no IPC). Every ``src`` module that touches MT5 imports
``mt5`` / ``_MT5_AVAILABLE`` from here instead of re-implementing the try/except
fallback, so the import-time contract is defined once.
"""

from __future__ import annotations

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    # EA-only master mode: the bridge never executes follower IPC when no
    # follower is activated, so the package is optional at import time. The
    # class definitions reference ORDER_TYPE_* while building ORDER_TYPE_MAP,
    # so provide the canonical enum values as a stand-in; runtime MT5 calls are
    # guarded by _MT5_AVAILABLE at the entry points.
    _MT5_AVAILABLE = False

    class _OrderTypeStub:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_TYPE_BUY_LIMIT = 2
        ORDER_TYPE_SELL_LIMIT = 3
        ORDER_TYPE_BUY_STOP = 4
        ORDER_TYPE_SELL_STOP = 5
        ORDER_TYPE_BUY_STOP_LIMIT = 6
        ORDER_TYPE_SELL_STOP_LIMIT = 7

    mt5 = _OrderTypeStub