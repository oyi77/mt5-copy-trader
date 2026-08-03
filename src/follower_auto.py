"""Auto-trading (Algo button) enablement for a follower terminal.

This is a mixin (no ``__init__``) — the composing executor provides ``_cfg``
(the FollowerConfig), ``_name``, and ``_exe_path``. The visible-window UI
automation itself lives in ``src/terminal_ui.py``; this mixin only orchestrates
it with the MT5 API.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

from src import terminal_ui
from src.mt5_compat import mt5

logger = logging.getLogger(__name__)


class AutoTradingMixin:
    """Ensure the follower MT5 terminal has auto-trading (Algo button) enabled."""

    def _enable_auto_trading(self) -> bool:
        """Ensure MT5 terminal has auto-trading (Algo button) enabled.

        The Algo button cannot be enabled via config files — it must be
        toggled via Ctrl+E in a visible terminal window. This method:
        1. Checks whether auto-trading is already on (no window needed)
        2. Tries a programmatic login (seeded session — no UI needed)
        3. Falls back to the visible-window dance: kill the hidden
           terminal, start ours visibly, wait for the window, toggle the
           Algo button (WM_COMMAND first, Ctrl+E via PowerShell as retry),
           and verify trade_allowed=True.
        """
        logger.info("%s: ensuring auto-trading is enabled...", self._name)

        if self._mt5_trade_allowed_initial():
            return True

        # Programmatic login FIRST: a terminal with a seeded session can be
        # bootstrapped purely via the API (mt5.initialize with credentials),
        # which avoids the fragile visible-window UI automation entirely. The
        # UI dance below is only a fallback for when programmatic login fails
        # (e.g. brand-new install with no account session at all).
        if self._cfg.login and self._enable_via_api_login():
            return True

        # Up to 3 attempts to ensure the terminal runs with a visible window
        hwnd = self._ensure_visible_terminal_window()
        if hwnd is None:
            logger.error("%s: terminal window not found after 3 attempts", self._name)
            return False

        # Small extra wait for the window to fully initialise
        time.sleep(2.0)

        # Dismiss any modal dialogs (Login, connection prompts) that might
        # block the main window from processing commands.
        terminal_ui.dismiss_blocking_dialogs(self._name, hwnd)

        # Find the process so the Ctrl+E fallback can target it by PID.
        our_pid = terminal_ui.find_terminal_pid(self._exe_path)
        if our_pid is None:
            logger.error("%s: terminal PID not found after window appeared", self._name)
            return False

        return self._toggle_algo_and_verify(hwnd, our_pid)

    def _mt5_trade_allowed_initial(self) -> bool:
        """True if auto-trading is already on (no visible window needed)."""
        init_ok = mt5.initialize(path=self._cfg.path, port=self._cfg.port, timeout=5000)
        if not init_ok:
            return False
        try:
            ti = mt5.terminal_info()
            if ti and ti.trade_allowed:
                logger.info(
                    "%s: auto-trading already enabled (trade_allowed=True)",
                    self._name,
                )
                return True
        finally:
            mt5.shutdown()
        return False

    def _enable_via_api_login(self) -> bool:
        """Try enabling auto-trading purely via the API (no UI).

        Returns True when the follower is now ready to trade (trade_allowed
        or skip_auto_trading); False means the UI fallback is required.
        """
        init_ok = mt5.initialize(
            path=self._cfg.path,
            port=self._cfg.port,
            login=self._cfg.login,
            password=self._cfg.password,
            server=self._cfg.server,
            timeout=15000,
        )
        if not init_ok:
            logger.warning(
                "%s: programmatic login failed: %s", self._name, mt5.last_error(),
            )
            return False
        try:
            ti = mt5.terminal_info()
            trade_ok = ti.trade_allowed if ti else False
            if trade_ok:
                logger.info(
                    "%s: auto-trading enabled via programmatic login "
                    "(trade_allowed=True)",
                    self._name,
                )
                return True
            if self._cfg.skip_auto_trading:
                logger.warning(
                    "%s: programmatic login ok but trade_allowed=%s, "
                    "skip_auto_trading=True — proceeding anyway",
                    self._name, trade_ok,
                )
                return True
            logger.warning(
                "%s: programmatic login ok but trade_allowed=%s",
                self._name, trade_ok,
            )
            return False
        finally:
            mt5.shutdown()

    def _ensure_visible_terminal_window(self) -> Optional[int]:
        """Kill any hidden terminal and start ours visibly, up to 3 attempts.

        Returns the terminal's main-frame hwnd, or None if no window could
        be obtained. Window enumeration lives in src/terminal_ui.py.
        """
        for attempt in range(1, 4):
            # Step 1: kill only the terminal64.exe matching OUR path
            our_pid = terminal_ui.find_terminal_pid(self._exe_path)
            if our_pid is not None:
                logger.info(
                    "%s: killing terminal PID %d (attempt %d/3)...",
                    self._name, our_pid, attempt,
                )
                terminal_ui.kill_terminal(our_pid)
                # Small wait for process cleanup
                time.sleep(2.0)
            else:
                logger.info(
                    "%s: no running terminal found for our path (attempt %d/3)",
                    self._name, attempt,
                )

            # Step 2: start terminal VISIBLY
            login_str = (
                f"/login:{self._cfg.login},{self._cfg.password},{self._cfg.server}"
            )
            try:
                logger.info(
                    "%s: starting terminal visibly (attempt %d/3)...",
                    self._name, attempt,
                )
                # shell=True ensures the window is visible (GUI process)
                subprocess.Popen([self._exe_path, login_str], shell=True)
            except Exception as e:
                logger.error(
                    "%s: failed to start terminal visibly: %s", self._name, e,
                )
                continue

            # Step 3: wait for any window belonging to the terminal process
            hwnd = terminal_ui.wait_for_terminal_window(
                self._exe_path, self._name, timeout=15.0,
            )
            if hwnd is not None:
                logger.info(
                    "%s: terminal window found (hwnd=%d)", self._name, hwnd,
                )
                return hwnd
            logger.warning(
                "%s: terminal window not found within 15s (attempt %d/3)",
                self._name, attempt,
            )
        return None

    def _toggle_algo_and_verify(self, hwnd: int, our_pid: int) -> bool:
        """Toggle the Algo button on a visible terminal and verify it.

        Uses WM_COMMAND first (non-intrusive), then verifies via the MT5
        API; if trade_allowed is still False, sends Ctrl+E via PowerShell
        and re-verifies once.
        """
        logger.info(
            "%s: sending WM_COMMAND algo toggle (hwnd=%d)...",
            self._name, hwnd,
        )
        terminal_ui.toggle_algo_via_wm_command(hwnd)

        # Connect to the EXISTING visible terminal (WITHOUT login first)
        init_ok = mt5.initialize(
            path=self._cfg.path,
            port=self._cfg.port,
            timeout=10000,
        )
        if not init_ok:
            logger.error(
                "%s: mt5.initialize() cannot connect to visible terminal: %s",
                self._name, mt5.last_error(),
            )
            mt5.shutdown()
        elif self._login_and_check():
            return True

        # Retry: send Ctrl+E once more and recheck
        logger.info("%s: sending Ctrl+E again and retrying...", self._name)
        terminal_ui.send_ctrl_e_via_powershell(our_pid, hwnd)
        time.sleep(1.5)

        retry_ok = mt5.initialize(
            path=self._cfg.path,
            port=self._cfg.port,
            timeout=10000,
        )
        if not retry_ok:
            logger.error(
                "%s: retry mt5.initialize() failed: %s", self._name, mt5.last_error(),
            )
            mt5.shutdown()
            return False
        if self._login_and_check():
            logger.info(
                "%s: auto-trading ENABLED after 2nd SendKeys", self._name,
            )
            return True
        logger.error("%s: could NOT enable auto-trading", self._name)
        return False

    def _login_and_check(self) -> bool:
        """mt5.login() then verify trade_allowed (checked before shutdown).

        Returns True when trade_allowed is on. A failed login or a False
        trade_allowed both return False and leave the terminal state for
        the next attempt.
        """
        login_ok = mt5.login(
            login=self._cfg.login,
            password=self._cfg.password,
            server=self._cfg.server,
        )
        if not login_ok:
            logger.error(
                "%s: mt5.login() failed: %s", self._name, mt5.last_error(),
            )
            mt5.shutdown()
            return False
        try:
            # Verify BEFORE shutting down — calling terminal_info() after
            # shutdown always returns None.
            ti = mt5.terminal_info()
            if ti and ti.trade_allowed:
                logger.info(
                    "%s: auto-trading ENABLED (trade_allowed=True)",
                    self._name,
                )
                return True
            logger.warning(
                "%s: trade_allowed=%s after visible start",
                self._name, ti.trade_allowed if ti else 'N/A',
            )
            return False
        finally:
            mt5.shutdown()