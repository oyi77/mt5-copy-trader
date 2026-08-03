"""Follower MT5 terminal trade execution.

``FollowerExecutor`` is the public entry point used by the bridge and agent
client. Its behaviour is split across focused mixin modules, one per concern:

- ``src/follower_mapping`` — symbol mapping, lot scaling, price rounding
- ``src/follower_auto`` — Algo-button (auto-trading) enablement
- ``src/follower_file`` — file-relay execution (pending.txt / result.txt)
- ``src/follower_risk`` — risk-limit enforcement (daily loss/trades, drawdown,
  max positions)
- ``src/follower_actions`` — IPC trade actions + dedup lookups
- ``src/follower_queue`` — disk-persisted replay queue

This module keeps only the constructor, lifecycle (launch/connect/disconnect),
the execute() dispatch, and status reporting.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Optional

from src.config import FollowerConfig
from src.models import TradeEvent
from src.mt5_compat import mt5, _MT5_AVAILABLE
from src.follower_actions import TradeActionsMixin
from src.follower_auto import AutoTradingMixin
from src.follower_file import FileRelayMixin
from src.follower_mapping import SymbolMappingMixin
from src.follower_queue import QueueMixin
from src.follower_risk import RiskLimitsMixin

logger = logging.getLogger(__name__)


class FollowerExecutor(
    FileRelayMixin,
    QueueMixin,
    AutoTradingMixin,
    TradeActionsMixin,
    RiskLimitsMixin,
    SymbolMappingMixin,
):
    """Connects to a follower MT5 terminal and executes trade events."""

    def __init__(self, config: FollowerConfig):
        self._cfg = config
        self._name = config.name
        self._process: Optional[subprocess.Popen] = None
        self._exe_path: str = config.path
        self._auto_trading_enabled: bool = False
        self._last_trade_allowed_fail: float = 0.0  # timestamp of last trade_allowed=False
        self._dry_run: bool = config.dry_run
        self._file_data_path: str = config.terminal_data_path.strip('"\' ') if config.terminal_data_path else ""
        # Serializes disk queue load/save/enqueue; reentrant because replay holds
        # it across execute() -> _enqueue_event.
        self._queue_lock = threading.RLock()
        # In-memory peak equity for true-drawdown risk checks (resets daily; not
        # persisted across restarts — documented tradeoff).
        self._peak_equity: float = 0.0
        self._peak_equity_date: str = ""
        # Cache of mt5.symbol_info() results per symbol (volume step / digits).
        self._symbol_info_cache: dict = {}
        # Ensure files dir exists
        if self.is_file_based():
            os.makedirs(self._file_data_path, exist_ok=True)
            if (self._cfg.max_daily_loss > 0.0
                    or self._cfg.max_daily_trades > 0
                    or self._cfg.max_drawdown_pct > 0.0):
                logger.warning(
                    "%s: risk limits configured (max_daily_loss=%.2f, "
                    "max_daily_trades=%d, max_drawdown_pct=%.1f) but file-based "
                    "mode cannot enforce them — limits will NOT be applied",
                    self._name, self._cfg.max_daily_loss,
                    self._cfg.max_daily_trades, self._cfg.max_drawdown_pct,
                )

    def is_file_based(self) -> bool:
        """True when using file-based trade relay (no IPC)."""
        return bool(self._file_data_path)

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def launch_terminal(self, timeout: float = 15.0) -> bool:
        """Ensure the follower's MT5 terminal process is running.

        Each follower uses its OWN MT5 installation at self._cfg.path
        with its own Manager API port. mt5.initialize() auto-starts the
        terminal if it isn't running.
        """
        if not _MT5_AVAILABLE:
            logger.error(
                "%s: MetaTrader5 package not installed — IPC execution unavailable "
                "(EA-only master mode)", self._name,
            )
            return False
        logger.info(
            "%s: launching terminal at %s port %d...",
            self._name, self._cfg.path, self._cfg.port,
        )
        result = mt5.initialize(
            path=self._cfg.path,
            port=self._cfg.port,
            login=self._cfg.login,
            password=self._cfg.password,
            server=self._cfg.server,
            timeout=int(timeout * 1000),
        )
        if not result:
            logger.error("%s: launch_terminal failed: %s", self._name, mt5.last_error())
            return False
        logger.info("%s: terminal started and logged in", self._name)
        mt5.shutdown()
        return True

    def connect(self, master_port: int = 0) -> bool:
        """Initialize MT5 API connection to THIS follower's OWN terminal.

        Each follower runs its own MT5 installation at its own path and port.
        The master_port parameter is ignored — the follower always uses
        its own configured path + port for true isolation.

        Two-step login: first connect to the terminal (no login), then
        send credentials via mt5.login(). This works on fresh terminals
        that haven't been logged in before.

        On first connection, also ensures the Algo Trading button is on
        (required for automated trade execution).
        """
        # ── File-based mode (Exness, no IPC) ──
        if self.is_file_based():
            logger.info(
                "%s: file-based mode (terminal_data_path=%s)",
                self._name, self._file_data_path,
            )
            return True

        port = self._cfg.port

        # If we recently failed due to trade_allowed=False, back off
        # to avoid churning terminal processes every 5 seconds.
        if self._last_trade_allowed_fail and time.time() - self._last_trade_allowed_fail < 30:
            return False

        # ── Auto-trading enablement (one-time, no mt5.initialize!) ──
        # IMPORTANT: never call mt5.initialize() before we start the terminal
        # VISIBLY — mt5.initialize() always starts the terminal in hidden mode,
        # making it impossible to toggle the Algo button via Ctrl+E.
        if not self._auto_trading_enabled:
            if self._cfg.skip_auto_trading:
                logger.info(
                    "%s: skip_auto_trading=True, assuming auto-trading is already on",
                    self._name,
                )
                self._auto_trading_enabled = True
            elif self._enable_auto_trading():
                self._auto_trading_enabled = True
            else:
                logger.warning(
                    "%s: auto-trading enablement failed, will retry next cycle",
                    self._name,
                )
                # Do NOT fall through to mt5.initialize() — that would start a
                # hidden terminal and make future enablement attempts harder.
                return False

        result = mt5.initialize(
            path=self._cfg.path,
            port=port,
            timeout=8000,
        )
        if result:
            # Connected to existing terminal — check if account matches
            ai = mt5.account_info()
            if ai and ai.login == self._cfg.login and ai.server == self._cfg.server:
                logger.info(
                    "%s: existing terminal already has target account %d@%s",
                    self._name, ai.login, ai.server,
                )
                ti = mt5.terminal_info()
                trade_ok = ti.trade_allowed if ti else False
                if self._cfg.skip_auto_trading or trade_ok:
                    logger.info(
                        "%s: connected (trade_allowed=%s, skip_auto_trading=%s)",
                        self._name, trade_ok, self._cfg.skip_auto_trading,
                    )
                    return True
                logger.warning(
                    "%s: trade_allowed=%s but skip_auto_trading=False",
                    self._name, trade_ok,
                )
                mt5.shutdown()
                return False
            # Different account — need to login
            login_result = mt5.login(
                login=self._cfg.login,
                password=self._cfg.password,
                server=self._cfg.server,
                timeout=10000,
            )
            if login_result:
                ti = mt5.terminal_info()
                if ti and ti.trade_allowed:
                    logger.info(
                        "%s: connected to existing terminal (port %d), trade_allowed=%s",
                        self._name, port, ti.trade_allowed,
                    )
                    return True
                else:
                    trade_ok = ti.trade_allowed if ti else False
                    if self._cfg.skip_auto_trading:
                        if not trade_ok:
                            logger.warning(
                                "%s: connected to existing terminal (port %d) but trade_allowed=%s, "
                                "skip_auto_trading=True — proceeding anyway",
                                self._name, port, trade_ok,
                            )
                        return True
                    logger.warning(
                        "%s: connected to existing terminal (port %d) but trade_allowed=%s — "
                        "shutting down, will retry",
                        self._name, port, trade_ok,
                    )
                    self._last_trade_allowed_fail = time.time()
                    mt5.shutdown()
                    return False
            else:
                logger.warning(
                    "%s: existing terminal at port %d but login failed (maybe wrong account), "
                    "will start fresh terminal",
                    self._name, port,
                )
                mt5.shutdown()
        else:
            logger.info(
                "%s: no existing terminal at port %d, will start one",
                self._name, port,
            )

        # Step 2: Start a fresh terminal WITH login credentials
        result = mt5.initialize(
            path=self._cfg.path,
            port=port,
            login=self._cfg.login,
            password=self._cfg.password,
            server=self._cfg.server,
            timeout=15000,
        )
        if result:
            ti = mt5.terminal_info()
            if ti and ti.trade_allowed:
                logger.info(
                    "%s: started fresh terminal (port %d), trade_allowed=%s",
                    self._name, port, ti.trade_allowed,
                )
                return True
            else:
                trade_ok = ti.trade_allowed if ti else False
                if self._cfg.skip_auto_trading:
                    if not trade_ok:
                        logger.warning(
                            "%s: started fresh terminal (port %d) but trade_allowed=%s, "
                            "skip_auto_trading=True — proceeding anyway",
                            self._name, port, trade_ok,
                        )
                    return True
                logger.warning(
                    "%s: started fresh terminal (port %d) but trade_allowed=%s — "
                    "shutting down, will retry",
                    self._name, port, trade_ok,
                )
                self._last_trade_allowed_fail = time.time()
                mt5.shutdown()
                return False

        logger.error("%s: init with login failed: %s", self._name, mt5.last_error())
        mt5.shutdown()
        return False

    def disconnect(self) -> None:
        """Shutdown MT5 API connection."""
        if self.is_file_based():
            return
        mt5.shutdown()

    # ------------------------------------------------------------------
    # Execute (with auto-connect/disconnect per call)
    # ------------------------------------------------------------------

    def execute(self, event: TradeEvent) -> bool:
        """Execute a single trade event on this follower.

        Auto-connects before and disconnects after.
        Returns True if the operation was submitted successfully.
        """
        if self.is_file_based():
            return self._file_execute_event(event)

        symbol = self._map_symbol(event.symbol)
        volume = self._apply_lot_scaling(event.volume, symbol)

        # ── Risk limit checks (for trade-opening actions) ──
        if event.action in ("open", "place"):
            # Replay/dedup safety: if we've ALREADY materialized this master
            # ticket (position for open, pending order for place), this is a
            # duplicate delivery (e.g. server replay after a reconnect) — treat
            # it as a silent no-op BEFORE the risk/count gates, so a duplicate
            # is not re-queued and flagged as an error.
            if not self.connect():
                logger.warning(
                    "%s: connect failed for %s ticket=%d, queuing event",
                    self._name, event.action, event.master_ticket,
                )
                self._enqueue_event(event)
                return False
            stale = False
            try:
                if event.action == "open":
                    already = self._find_position_by_comment(str(event.master_ticket))
                else:
                    already = self._find_order_by_comment(str(event.master_ticket))
                # The current position/order check above only catches duplicates
                # while the copy is STILL OPEN. If the round trip already
                # completed (position or pending order closed) and a stale
                # OPEN/PLACE is re-delivered — hub replay after reconnect, or a
                # queue entry surviving a crash — the ticket now appears only
                # in deal/order HISTORY. A history record carrying our magic
                # and this master ticket means the event was already
                # materialized in an earlier delivery, so the duplicate is a
                # silent no-op. Without this, a stale replay re-opens a
                # position the master has long since closed.
                stale = already is None and self._history_has_ticket(
                    str(event.master_ticket)
                )
            finally:
                self.disconnect()
            if already is not None:
                logger.info(
                    "%s: %s for ticket %d already materialised, skipping duplicate",
                    self._name, event.action, event.master_ticket,
                )
                return True
            if stale:
                logger.info(
                    "%s: %s for ticket %d already executed in a completed "
                    "round trip, skipping stale duplicate",
                    self._name, event.action, event.master_ticket,
                )
                return True

            if not self._check_risk_limits():
                logger.warning(
                    "%s: risk limits exceeded for %s ticket=%d, queuing event",
                    self._name, event.action, event.master_ticket,
                )
                self._enqueue_event(event)
                return False
            if not self._positions_below_max():
                logger.warning(
                    "%s: max_positions reached for %s ticket=%d, queuing event",
                    self._name, event.action, event.master_ticket,
                )
                self._enqueue_event(event)
                return False

        if not self.connect():
            logger.warning(
                "%s: connect failed for %s ticket=%d, queuing event",
                self._name, event.action, event.master_ticket,
            )
            self._enqueue_event(event)
            return False
        try:
            if event.action == "open":
                success = self._open(symbol, volume, event)
            elif event.action == "close":
                success = self._close(symbol, event)
            elif event.action == "modify":
                success = self._modify(symbol, event, volume)
            elif event.action == "place":
                success = self._place_order(symbol, volume, event)
            elif event.action == "modify_order":
                success = self._modify_order(symbol, event)
            elif event.action == "delete":
                success = self._delete_order(symbol, event)
            else:
                logger.error("%s: Unknown action %s", self._name, event.action)
                return False

            # Queue event for replay on failure (but not for close/modify/delete)
            if not success and event.action in ("open", "place"):
                self._enqueue_event(event)

            return success
        finally:
            self.disconnect()

    def get_status(self) -> dict:
        """Return status dict for dashboard display."""
        if self.is_file_based():
            return self._file_get_status()

        info = {"name": self._name, "active": True}
        try:
            ok = mt5.initialize(path=self._exe_path, port=self._cfg.port, timeout=2000)
            info["connected"] = ok
            if ok:
                acc = mt5.account_info()
                ti = mt5.terminal_info()
                if acc:
                    info["balance"] = acc.balance
                    info["equity"] = acc.equity
                    info["login"] = acc.login
                    info["server"] = acc.server
                info["trade_allowed"] = ti.trade_allowed if ti else None
            mt5.shutdown()
        except Exception:
            info["connected"] = False
        finally:
            try:
                mt5.shutdown()
            except Exception:
                pass
        return info