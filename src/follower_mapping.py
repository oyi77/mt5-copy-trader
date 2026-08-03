"""Symbol mapping, lot scaling, and price-rounding helpers for a follower.

This is a mixin (no ``__init__``) — the composing executor provides the
``_cfg`` config object and the ``_symbol_info_cache`` dict. Kept in its own
module because these helpers are shared by the IPC execution path and the
file-relay path, and they are pure mapping logic with a single responsibility.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.mt5_compat import mt5

logger = logging.getLogger(__name__)


class SymbolMappingMixin:
    """Helpers mapping master symbols / lots / prices onto a follower account."""

    def _map_symbol(self, symbol: str) -> str:
        """Map a master symbol name to one available on this account.

        Resolution order:
        1. explicit symbol_mapping config
        2. the exact symbol, if it exists on this account
        3. suffix variants brokers/account-groups rename symbols with:
           'c' (Exness metals/indices, e.g. XAUUSDc) and 'm' (Exness
           Standard/micro account groups, e.g. BTCUSDm, EURUSDm).

        Probes assume a live MT5 connection (callers that map from a
        disconnected context must use the explicit symbol_mapping config).
        """
        mapping = self._cfg.symbol_mapping
        upper = symbol.upper()
        if upper in mapping:
            return mapping[upper]
        # Prefer the exact symbol if it exists on this account.
        try:
            info = mt5.symbol_info(upper)
        except Exception:
            info = None
        if info is not None:
            return upper
        # Account-group suffix variants. Each candidate is checked for
        # existence; first hit wins.
        for suffix in ("c", "m", ".a", "a", "USDc", "USDm"):
            cand = upper + suffix
            if upper.endswith(suffix):
                continue
            try:
                c_info = mt5.symbol_info(cand)
            except Exception:
                c_info = None
            if c_info is not None:
                logger.info(
                    "%s: auto-mapped %s -> %s (account symbol variant)",
                    self._name, upper, cand,
                )
                return cand
        return upper

    def _symbol_info_cached(self, symbol: str):
        """Return cached mt5.symbol_info() result for the symbol (or None).

        Probes MT5 once per symbol; a failed probe is cached as None so file
        mode (uninitialized MT5) doesn't re-probe on every call.
        """
        if symbol not in self._symbol_info_cache:
            try:
                self._symbol_info_cache[symbol] = mt5.symbol_info(symbol)
            except Exception:
                self._symbol_info_cache[symbol] = None
        return self._symbol_info_cache[symbol]

    def _apply_lot_scaling(self, volume: float, symbol: Optional[str] = None) -> float:
        """Scale master volume by lot_multiplier, clamp into [min_lot, max_lot],
        and round to the symbol's volume step when obtainable (else 2 decimals)."""
        vol = volume * self._cfg.lot_multiplier
        if self._cfg.max_lot > 0:
            vol = min(vol, self._cfg.max_lot)
        if self._cfg.min_lot > 0:
            vol = max(vol, self._cfg.min_lot)
        step = None
        if symbol:
            info = self._symbol_info_cached(symbol)
            step = info.volume_step if info is not None else None
        if step and step > 0:
            # Round to a multiple of the step, then strip float dust
            # (e.g. 100 * 0.01 -> 1.0000000000000002) with a final 10-decimal round.
            vol = round(round(vol / step) * step, 10)
        else:
            vol = round(vol, 2)
        # Step rounding may push vol below min_lot (or above max_lot) — re-clamp.
        if self._cfg.min_lot > 0 and vol < self._cfg.min_lot:
            vol = self._cfg.min_lot
        if self._cfg.max_lot > 0 and vol > self._cfg.max_lot:
            vol = self._cfg.max_lot
        return vol

    def _map_price(self, price: float, symbol: Optional[str] = None) -> float:
        """Round price to the symbol's digits when obtainable (via symbol_info);
        fall back to 3 for JPY-style quotes (price >= 100), else 5."""
        if symbol:
            info = self._symbol_info_cached(symbol)
            if info is not None and info.digits:
                return round(price, info.digits)
        return round(price, 3) if price >= 100 else round(price, 5)