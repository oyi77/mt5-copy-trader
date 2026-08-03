"""Follower MT5 terminal trade execution."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from ctypes import wintypes
from typing import Optional

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    # EA-only master mode: the bridge never executes follower IPC when no
    # follower is activated, so the package is optional at import time. The
    # class definitions below reference ORDER_TYPE_* while building
    # ORDER_TYPE_MAP, so provide the canonical enum values as a stand-in;
    # runtime MT5 calls are guarded by _MT5_AVAILABLE at the entry points.
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

from src.config import FollowerConfig
from src.models import TradeEvent

logger = logging.getLogger(__name__)

# Windows API user32.dll — used by _wait_for_terminal_window
_user32 = ctypes.WinDLL('user32', use_last_error=True)


_WM_COMMAND = 0x0111
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_VK_ESCAPE = 27
_VK_RETURN = 13
_ALGO_CMD_IDS = (33051, 33050, 33052, 32808)  # known MT5 Algo command IDs


def _dismiss_blocking_dialogs(name: str, main_hwnd: int) -> None:
    """Close any visible modal dialogs (Login, connection prompts) that
    belong to the same process as main_hwnd and might block input.

    Uses multiple approaches:
    - WM_CLOSE (standard close)
    - WM_COMMAND(IDCANCEL=2) (dialog cancel button)
    - WM_DESTROY (forceful destroy)
    - Simulated Escape key (WM_KEYDOWN/WM_KEYUP)
    - Simulated Enter key (to press default OK button)
    """
    from ctypes import wintypes as _wt
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _GetWindowThreadProcessId.argtypes = [_wt.HWND, ctypes.POINTER(_wt.DWORD)]
    _GetWindowThreadProcessId.restype = _wt.DWORD

    # Get the PID of the main window
    pid = _wt.DWORD()
    _GetWindowThreadProcessId(main_hwnd, ctypes.byref(pid))
    target_pid = pid.value

    if not target_pid:
        return

    _PostMessageW = _user32.PostMessageW
    _PostMessageW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    _PostMessageW.restype = _wt.BOOL
    _GetWindowTextW = _user32.GetWindowTextW
    _GetClassNameW = _user32.GetClassNameW
    _IsWindowVisible = _user32.IsWindowVisible
    _title_buf = ctypes.create_unicode_buffer(256)
    _class_buf = ctypes.create_unicode_buffer(256)
    _pid_buf = _wt.DWORD()

    _target_pid_ref = [target_pid]
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)

    def _close_proc(hwnd, lparam):
        _GetWindowThreadProcessId(hwnd, ctypes.byref(_pid_buf))
        if _pid_buf.value != _target_pid_ref[0]:
            return 1
        _GetClassNameW(hwnd, _class_buf, 256)
        cls = _class_buf.value
        # Class #32770 = standard dialog window
        if cls == '#32770':
            _GetWindowTextW(hwnd, _title_buf, 256)
            title = _title_buf.value
            if not _IsWindowVisible(hwnd):
                return 1
            logger.info(
                "%s: closing visible dialog hwnd=%d title='%s'",
                name, hwnd, title,
            )
            # Fire ALL close mechanisms at once (best effort)
            _PostMessageW(hwnd, _WM_CLOSE, 0, 0)       # standard close
            _PostMessageW(hwnd, _WM_COMMAND, 2, 0)     # IDCANCEL
            _PostMessageW(hwnd, _WM_DESTROY, 0, 0)     # forceful destroy
            _PostMessageW(hwnd, _WM_KEYDOWN, _VK_ESCAPE, 0)  # Esc key press
            _PostMessageW(hwnd, _WM_KEYUP, _VK_ESCAPE, 0)    # Esc key release
            _PostMessageW(hwnd, _WM_KEYDOWN, _VK_RETURN, 0)  # Enter key press
            _PostMessageW(hwnd, _WM_KEYUP, _VK_RETURN, 0)    # Enter key release
        return 1

    cb = _WNDENUMPROC(_close_proc)
    _user32.EnumWindows(cb, 0)


def _toggle_algo_via_wm_command(hwnd: int) -> bool:
    """Toggle the Algo button by sending WM_COMMAND directly.

    Sends WM_COMMAND messages with known Algo button command IDs
    directly to the main MetaTrader frame window via PostMessageW.
    This bypasses keyboard/focus/foreground issues — it works even
    when the window is not in the foreground.
    """
    from ctypes import wintypes as _wt
    _PostMessageW = _user32.PostMessageW
    _PostMessageW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    _PostMessageW.restype = _wt.BOOL

    sent_any = False
    for cmd_id in _ALGO_CMD_IDS:
        # Send multiple times to ensure delivery
        for _ in range(3):
            result = _PostMessageW(hwnd, _WM_COMMAND, cmd_id, 0)
            if result:
                sent_any = True
        if sent_any:
            logger.info("Sent WM_COMMAND %d to hwnd=%d", cmd_id, hwnd)
    return sent_any


def _send_ctrl_e_via_powershell(pid: int, hwnd: int = 0) -> None:
    """Send Ctrl+E via PowerShell SetForegroundWindow+SendKeys.

    WARNING: This brings the terminal window to the foreground (intrusive).
    Only use as LAST resort when WM_COMMAND doesn't work.
    """
    if hwnd:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$def = '[DllImport(\"user32.dll\")]"
            "public static extern bool SetForegroundWindow(IntPtr hWnd);';"
            "Add-Type -MemberDefinition $def -Name W32 -Namespace W;"
            "[W.W32]::SetForegroundWindow([IntPtr]%d);"
            "Start-Sleep -Milliseconds 500;"
            "[System.Windows.Forms.SendKeys]::SendWait('^(e)');"
            "Start-Sleep -Milliseconds 1000;"
            "[System.Windows.Forms.SendKeys]::SendWait('^(e)');"
            "Start-Sleep -Milliseconds 1000;"
            "[System.Windows.Forms.SendKeys]::SendWait('^(e)');"
            "Write-Output 'OK';"
        ) % hwnd
    else:
        # Fallback: AppActivate by PID
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            "try {"
            "  [Microsoft.VisualBasic.Interaction]::AppActivate(%d);"
            "  Start-Sleep -Milliseconds 500;"
            "  [System.Windows.Forms.SendKeys]::SendWait('^(e)');"
            "  Start-Sleep -Milliseconds 1000;"
            "  [System.Windows.Forms.SendKeys]::SendWait('^(e)');"
            "  Start-Sleep -Milliseconds 1000;"
            "  [System.Windows.Forms.SendKeys]::SendWait('^(e)');"
            "  Write-Output 'OK';"
            "} catch { Write-Error $_.Exception.Message; }"
        ) % pid
    import subprocess as _sp
    try:
        result = _sp.check_output(
            ['powershell', '-STA', '-ExecutionPolicy', 'Bypass',
             '-Command', ps_script],
            timeout=15, stderr=_sp.STDOUT,
        )
        logger.info(
            "PowerShell SendKeys result: %s",
            result.decode('utf-8', errors='replace').strip(),
        )
    except Exception as e:
        logger.warning("PowerShell SendKeys failed: %s", e)


class FollowerExecutor:
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

    # ------------------------------------------------------------------
    # Auto-trading enabler
    # ------------------------------------------------------------------

    def _enable_auto_trading(self) -> bool:
        """Ensure MT5 terminal has auto-trading (Algo button) enabled.

        The Algo button cannot be enabled via config files — it must be
        toggled via Ctrl+E in a visible terminal window. This method:
        1. Kills any existing hidden terminal (started by mt5.initialize)
        2. Starts the terminal VISIBLY
        3. Waits for the window to appear
        4. Sends Ctrl+E to toggle the Algo button
        5. Verifies trade_allowed=True
        """
        logger.info("%s: ensuring auto-trading is enabled...", self._name)

        # First check if auto-trading is already enabled (no window needed)
        init_ok = mt5.initialize(path=self._cfg.path, port=self._cfg.port, timeout=5000)
        if init_ok:
            ti = mt5.terminal_info()
            if ti and ti.trade_allowed:
                logger.info(
                    "%s: auto-trading already enabled (trade_allowed=True)",
                    self._name,
                )
                mt5.shutdown()
                return True
            mt5.shutdown()

        # Programmatic login FIRST: a terminal with a seeded session can be
        # bootstrapped purely via the API (mt5.initialize with credentials),
        # which avoids the fragile visible-window UI automation entirely. The
        # UI dance below is only a fallback for when programmatic login fails
        # (e.g. brand-new install with no account session at all).
        if self._cfg.login:
            init_ok = mt5.initialize(
                path=self._cfg.path,
                port=self._cfg.port,
                login=self._cfg.login,
                password=self._cfg.password,
                server=self._cfg.server,
                timeout=15000,
            )
            if init_ok:
                ti = mt5.terminal_info()
                trade_ok = ti.trade_allowed if ti else False
                if trade_ok:
                    logger.info(
                        "%s: auto-trading enabled via programmatic login "
                        "(trade_allowed=True)",
                        self._name,
                    )
                    mt5.shutdown()
                    return True
                mt5.shutdown()
                if self._cfg.skip_auto_trading:
                    logger.warning(
                        "%s: programmatic login ok but trade_allowed=%s, "
                        "skip_auto_trading=True — proceeding anyway",
                        self._name, trade_ok,
                    )
                    return True
                logger.warning(
                    "%s: programmatic login ok but trade_allowed=%s — "
                    "falling back to UI toggle",
                    self._name, trade_ok,
                )
            else:
                logger.warning(
                    "%s: programmatic login failed: %s — falling back to UI toggle",
                    self._name, mt5.last_error(),
                )

        import subprocess as _sp

        # Up to 3 attempts to ensure the terminal runs with a visible window
        for attempt in range(1, 4):
            # Step 1: kill only the terminal64.exe matching OUR path
            our_pid = self._find_terminal_pid()
            if our_pid is not None:
                logger.info(
                    "%s: killing terminal PID %d (attempt %d/3)...",
                    self._name, our_pid, attempt,
                )
                _sp.call(
                    ['taskkill', '/F', '/PID', str(our_pid)],
                    timeout=5, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
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
                _sp.Popen([self._exe_path, login_str], shell=True)
            except Exception as e:
                logger.error(
                    "%s: failed to start terminal visibly: %s", self._name, e,
                )
                continue

            # Step 3: wait for any window belonging to the terminal process
            hwnd = self._wait_for_terminal_window(timeout=15.0)
            if hwnd is not None:
                logger.info(
                    "%s: terminal window found (hwnd=%d)", self._name, hwnd,
                )
                break
            logger.warning(
                "%s: terminal window not found within 15s (attempt %d/3)",
                self._name, attempt,
            )
        else:
            logger.error("%s: terminal window not found after 3 attempts", self._name)
            return False

        # Small extra wait for the window to fully initialise
        time.sleep(2.0)

        # Step 3b: dismiss any modal dialogs (Login, connection prompts)
        # that might block the main window from processing commands
        _dismiss_blocking_dialogs(self._name, hwnd)

        # Step 4a: try WM_COMMAND first (non-intrusive, no flash/focus steal)
        our_pid = self._find_terminal_pid()
        if our_pid is None:
            logger.error("%s: terminal PID not found after window appeared", self._name)
            return False
        logger.info(
            "%s: sending WM_COMMAND algo toggle (hwnd=%d)...",
            self._name, hwnd,
        )
        wm_worked = _toggle_algo_via_wm_command(hwnd)
        # Step 5: connect to the EXISTING visible terminal (WITHOUT login first)
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
        else:
            # Now login to switch accounts
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
            else:
                # Verify trade_allowed BEFORE shutting down — calling
                # terminal_info() after shutdown always returns None.
                ti = mt5.terminal_info()
                if ti and ti.trade_allowed:
                    logger.info(
                        "%s: auto-trading ENABLED (trade_allowed=True)",
                        self._name,
                    )
                    mt5.shutdown()
                    return True
                elif ti:
                    logger.warning(
                        "%s: trade_allowed=%s after visible start, retrying...",
                        self._name, ti.trade_allowed,
                    )
                else:
                    logger.warning(
                        "%s: terminal_info() returned None", self._name,
                    )
                mt5.shutdown()

            # Retry: send Ctrl+E once more and recheck
            logger.info("%s: sending Ctrl+E again and retrying...", self._name)
            _send_ctrl_e_via_powershell(our_pid, hwnd)
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
            else:
                retry_login = mt5.login(
                    login=self._cfg.login,
                    password=self._cfg.password,
                    server=self._cfg.server,
                )
                if not retry_login:
                    logger.error(
                        "%s: retry mt5.login() failed: %s", self._name, mt5.last_error(),
                    )
                    mt5.shutdown()
                else:
                    # Verify BEFORE shutting down (terminal_info after shutdown
                    # is always None -> spurious "still False" warning).
                    ti2 = mt5.terminal_info()
                    if ti2 and ti2.trade_allowed:
                        logger.info(
                            "%s: auto-trading ENABLED after 2nd SendKeys",
                            self._name,
                        )
                        mt5.shutdown()
                        return True
                    logger.warning(
                        "%s: trade_allowed still False after 2nd SendKeys (val=%s)",
                        self._name, ti2.trade_allowed if ti2 else 'N/A',
                    )
                    mt5.shutdown()

        logger.error("%s: could NOT enable auto-trading", self._name)
        return False

    def _find_terminal_pid(self) -> Optional[int]:
        """Return PID of running terminal64.exe that matches our path, or None.

        Uses WMIC without shell=True for reliable execution.
        """
        import subprocess as _sp
        try:
            output = _sp.check_output(
                ['wmic', 'process', 'where', "name='terminal64.exe'",
                 'get', 'processid,executablepath', '/format:csv'],
                timeout=5,
            ).decode('utf-8', errors='replace')
            # Parse CSV lines; first line is header, second may be blank
            for line in output.strip().split('\n'):
                line = line.strip()
                if not line or line.startswith('Node'):
                    continue
                parts = line.split(',')
                if len(parts) >= 3:
                    exe_path = parts[-2].strip()
                    pid_str = parts[-1].strip()
                    if exe_path and pid_str and pid_str.isdigit():
                        # Normalize both paths for comparison
                        our_path = self._exe_path.replace('/', '\\').lower()
                        if our_path in exe_path.lower():
                            logger.debug(
                                "%s: found terminal PID %s at %s",
                                self._name, pid_str, exe_path,
                            )
                            return int(pid_str)
        except Exception as e:
            logger.debug("%s: wmic lookup failed: %s", self._name, e)
        return None

    def _wait_for_terminal_window(self, timeout: float = 20.0) -> Optional[int]:
        """Wait for any visible top-level window belonging to terminal64.exe.

        More robust than FindWindowW by class name — enumerates all top-level
        windows and checks each one's owning process via GetWindowThreadProcessId.
        """
        from ctypes import wintypes as _wt

        _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
        _GetWindowThreadProcessId.argtypes = [_wt.HWND, ctypes.POINTER(_wt.DWORD)]
        _GetWindowThreadProcessId.restype = _wt.DWORD
        _IsWindowVisible = _user32.IsWindowVisible
        _IsWindowVisible.argtypes = [_wt.HWND]
        _IsWindowVisible.restype = _wt.BOOL

        def _get_terminal_pids() -> list:
            """Return list of PIDs of terminal64.exe matching our path."""
            pids = []
            our_path = self._exe_path.replace('/', '\\').lower()
            try:
                import subprocess as _sp
                out = _sp.check_output(
                    ['wmic', 'process', 'where', "name='terminal64.exe'",
                     'get', 'processid,executablepath', '/format:csv'],
                    timeout=5,
                )
                out = out.decode('utf-8', errors='replace')
                for line in out.strip().split('\n'):
                    line = line.strip()
                    if not line or line.startswith('Node'):
                        continue
                    parts = line.split(',')
                    if len(parts) >= 3:
                        exe_path = parts[-2].strip()
                        pid_str = parts[-1].strip()
                        if exe_path and pid_str and pid_str.isdigit():
                            if our_path in exe_path.lower():
                                pids.append(int(pid_str))
            except Exception:
                pass
            return pids

        # Callback for EnumWindows — uses outer-scope pids/hwnd variables
        _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)
        _pid_buf = _wt.DWORD()
        _title_buf = ctypes.create_unicode_buffer(256)
        _class_buf = ctypes.create_unicode_buffer(256)
        _GetClassNameW = _user32.GetClassNameW
        _GetClassNameW.argtypes = [_wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
        _GetClassNameW.restype = ctypes.c_int

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            terminal_pids = _get_terminal_pids()
            if not terminal_pids:
                time.sleep(0.5)
                continue

            found_metatrader = [None]  # mutable container for closure
            found_any = [None]  # any non-trash window as fallback

            def _enum_proc(hwnd, lparam):
                _GetWindowThreadProcessId(hwnd, ctypes.byref(_pid_buf))
                if _pid_buf.value in terminal_pids:
                    # Get title and class
                    _user32.GetWindowTextW(hwnd, _title_buf, 256)
                    _GetClassNameW(hwnd, _class_buf, 256)
                    title = _title_buf.value
                    cls = _class_buf.value
                    logger.info(
                        "%s: found terminal window hwnd=%d title='%s' class='%s'",
                        self._name, hwnd, title, cls,
                    )
                    # Ignore GDI+ hook, IME, and tooltip windows (not real UI)
                    if cls.startswith(('GDI+', 'tooltips_class32', 'MSCTFIME UI', 'ComboLBox')):
                        return 1
                    if cls == 'IME':
                        return 1
                    # Show the window if hidden
                    if not _IsWindowVisible(hwnd):
                        _user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
                    # MetaTrader class = main frame window (what we want)
                    if 'MetaTrader' in cls or 'MetaTrader' in title:
                        if found_metatrader[0] is None:
                            found_metatrader[0] = hwnd
                    # First non-trash window as fallback
                    if found_any[0] is None:
                        found_any[0] = hwnd
                return 1

            cb = _WNDENUMPROC(_enum_proc)
            _user32.EnumWindows(cb, 0)

            # If we found a MetaTrader class window, return immediately
            if found_metatrader[0] is not None:
                logger.info(
                    "%s: selected main frame window hwnd=%d",
                    self._name, found_metatrader[0],
                )
                return found_metatrader[0]

            # Keep polling — don't return a fallback until deadline
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.5, max(0.1, remaining)))
                continue

            # Deadline reached — use fallback if available
            if found_any[0] is not None:
                logger.info(
                    "%s: deadline reached, using fallback window hwnd=%d",
                    self._name, found_any[0],
                )
                return found_any[0]

            time.sleep(0.5)

        return None

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

    def _check_risk_limits(self) -> bool:
        """Check risk limits against MT5 account history for today.

        Enforces:
        - max_daily_loss: total loss since midnight exceeds this threshold
        - max_drawdown_pct: current equity drawdown from peak exceeds this %
        - max_daily_trades: number of distinct positions traded today

        Returns True if all limits satisfied (or unchecked), False if any exceeded.
        """
        if self.is_file_based():
            # No MT5 API in file mode; limits are NOT enforced (a one-time
            # warning is logged at construction when limits are configured).
            return True

        if not self.connect():
            return True  # can't check, allow trade

        try:
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            # Get all deals today (closed trades + results)
            history = mt5.history_deals_get(
                today_start,
                datetime.now() + timedelta(seconds=1),
            )
            if history is not None and len(history) > 0:
                today_pnl = sum(deal.profit for deal in history)
                # Count distinct positions — a round trip (open+close) produces
                # 2 deals, so counting deals would double-count trades. Deals
                # without a position (deposits/withdrawals, position_id==0) are
                # not trades.
                today_trades = len(
                    {d.position_id for d in history if d.position_id > 0}
                )

                # Max daily loss (check only when P&L is negative)
                if self._cfg.max_daily_loss > 0.0 and today_pnl < 0:
                    loss_abs = abs(today_pnl)
                    if loss_abs >= self._cfg.max_daily_loss:
                        logger.warning(
                            "%s: daily loss %.2f exceeds limit %.2f -- blocking trade",
                            self._name, loss_abs, self._cfg.max_daily_loss,
                        )
                        return False

                # Max daily trades
                if self._cfg.max_daily_trades > 0:
                    if today_trades >= self._cfg.max_daily_trades:
                        logger.warning(
                            "%s: daily trades %d >= limit %d -- blocking trade",
                            self._name, today_trades, self._cfg.max_daily_trades,
                        )
                        return False

            # Max drawdown from peak equity (true drawdown, not balance-based).
            # Peak is tracked in-memory per process and resets each day; it is
            # NOT persisted across restarts (documented tradeoff).
            if self._cfg.max_drawdown_pct > 0.0:
                acc = mt5.account_info()
                if acc and acc.equity > 0:
                    equity = acc.equity
                    today = today_start.strftime("%Y-%m-%d")
                    if self._peak_equity_date != today:
                        self._peak_equity = equity
                        self._peak_equity_date = today
                    elif equity > self._peak_equity:
                        self._peak_equity = equity
                    if self._peak_equity > 0:
                        drawdown_pct = (
                            (self._peak_equity - equity) / self._peak_equity * 100.0
                        )
                        if drawdown_pct >= self._cfg.max_drawdown_pct:
                            logger.warning(
                                "%s: drawdown %.1f%% from peak %.2f >= limit %.1f%% -- blocking trade",
                                self._name, drawdown_pct, self._peak_equity,
                                self._cfg.max_drawdown_pct,
                            )
                            return False

        except Exception as e:
            logger.warning(
                "%s: risk check error (allowing trade): %s", self._name, e,
            )
        finally:
            self.disconnect()

        return True

    def _positions_below_max(self) -> bool:
        """True if open positions on the follower account are under max_positions.

        Only positions this follower itself opened (its configured magic) are
        counted — on shared master+follower accounts the master's positions
        must not consume the follower's copy cap. max_positions <= 0 disables
        the cap. File-relay mode cannot count positions via the API, so the cap
        is not enforced there (mirrors _check_risk_limits behaviour).
        """
        if self._cfg.max_positions <= 0 or self.is_file_based():
            return True
        if not self.connect():
            return True  # can't check, allow trade
        try:
            positions = mt5.positions_get() or []
            count = sum(1 for p in positions if p.magic == self._cfg.magic)
            if count >= self._cfg.max_positions:
                logger.warning(
                    "%s: open positions %d >= max_positions %d -- blocking OPEN",
                    self._name, count, self._cfg.max_positions,
                )
                return False
            return True
        except Exception as e:
            logger.warning(
                "%s: position count error (allowing trade): %s", self._name, e,
            )
            return True
        finally:
            self.disconnect()

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

    # ------------------------------------------------------------------
    # Trade actions
    # ------------------------------------------------------------------

    def _open(self, symbol: str, volume: float, event: TradeEvent) -> bool:
        # Replay safety: skip if position with this comment already exists
        existing = self._find_position_by_comment(str(event.master_ticket))
        if existing is not None:
            logger.info("%s: position for ticket %d already exists (sl=%.5f tp=%.5f), skipping",
                        self._name, event.master_ticket, existing.sl, existing.tp)
            return True

        order_type = mt5.ORDER_TYPE_BUY if event.position_type == 0 else mt5.ORDER_TYPE_SELL
        price = self._get_price(symbol, order_type)
        if price is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
            "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": str(event.master_ticket),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: OPEN %s %.2f %s (ticket=%d, comment=%s)",
                self._name, symbol, volume,
                "BUY" if event.position_type == 0 else "SELL",
                result.order, event.master_ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            comment = result.comment if result else ""
            logger.error(
                "%s: OPEN failed — retcode=%d comment=%s request=%s",
                self._name, retcode, comment, self._mask_request(request),
            )
            return False

    def _close(self, symbol: str, event: TradeEvent) -> bool:
        # Find follower position that matches this master ticket
        fpos = self._find_position_by_comment(str(event.master_ticket))
        if fpos is None:
            if self._history_has_ticket(str(event.master_ticket)):
                # Round trip already completed — this is a stale re-delivery
                # of a close (e.g. hub replay after reconnect). The desired
                # end state already holds; treat it as a silent success.
                logger.info(
                    "%s: CLOSE — ticket %d already closed (round trip complete), "
                    "stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: CLOSE — no follower position found for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        close_type = mt5.ORDER_TYPE_SELL if fpos.type == 0 else mt5.ORDER_TYPE_BUY
        price = self._get_price(symbol, close_type)
        if price is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": fpos.ticket,
            "volume": fpos.volume,
            "type": close_type,
            "price": price,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": f"close_{event.master_ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: CLOSE %s %.2f (master_ticket=%d, f_ticket=%d)",
                self._name, symbol, fpos.volume,
                event.master_ticket, fpos.ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            comment = result.comment if result else ""
            logger.error(
                "%s: CLOSE failed — retcode=%d comment=%s",
                self._name, retcode, comment,
            )
            return False

    def _modify(self, symbol: str, event: TradeEvent, current_volume: float) -> bool:
        fpos = self._find_position_by_comment(str(event.master_ticket))
        if fpos is None:
            if self._history_has_ticket(str(event.master_ticket)):
                logger.info(
                    "%s: MODIFY — ticket %d already closed (round trip complete), "
                    "stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: MODIFY — no follower position for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        # 1) Partial close if volume decreased
        close_vol = event.volume_change()
        if close_vol:
            # Scale the master-side delta by the follower's lot multiplier
            # (clamped to min/max lot), matching how the open volume is scaled.
            close_vol = self._apply_lot_scaling(close_vol, symbol)
            if not self._partial_close(symbol, fpos, close_vol, event):
                # Abort — proceeding would modify SL/TP on a position that
                # still holds the old volume, silently desyncing sizes.
                logger.warning(
                    "%s: MODIFY — partial close failed, aborting (SL/TP untouched)",
                    self._name,
                )
                return False
            fpos = self._find_position_by_comment(str(event.master_ticket))
            if fpos is None:
                return False

        # 2) SL/TP modification
        if (event.sl is not None and event.sl != fpos.sl) or \
           (event.tp is not None and event.tp != fpos.tp):
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": fpos.ticket,
                "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
                "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
                "magic": self._cfg.magic,
                "comment": fpos.comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = self._order_send(request)
            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                retcode = result.retcode if result else -1
                logger.error(
                    "%s: MODIFY SL/TP failed — retcode=%d",
                    self._name, retcode,
                )
                return False

        logger.info(
            "%s: MODIFY %s %.2f (master_ticket=%d)",
            self._name, symbol, current_volume, event.master_ticket,
        )
        return True

    # ------------------------------------------------------------------
    # Pending order actions
    # ------------------------------------------------------------------

    ORDER_TYPE_MAP = {
        2: mt5.ORDER_TYPE_BUY_LIMIT,
        3: mt5.ORDER_TYPE_SELL_LIMIT,
        4: mt5.ORDER_TYPE_BUY_STOP,
        5: mt5.ORDER_TYPE_SELL_STOP,
        6: mt5.ORDER_TYPE_BUY_STOP_LIMIT,
        7: mt5.ORDER_TYPE_SELL_STOP_LIMIT,
    }

    def _place_order(self, symbol: str, volume: float, event: TradeEvent) -> bool:
        # Replay safety: skip if pending order with this comment already exists
        existing = self._find_order_by_comment(str(event.master_ticket))
        if existing is not None:
            logger.info("%s: pending order for ticket %d already exists, skipping",
                        self._name, event.master_ticket)
            return True

        order_type = self.ORDER_TYPE_MAP.get(event.order_type)
        if order_type is None:
            logger.error(
                "%s: PLACE — unknown order type %s (expected 2-7), aborting",
                self._name, event.order_type,
            )
            return False

        price = event.price
        if price <= 0:
            logger.error("%s: PLACE — invalid price %.5f", self._name, price)
            return False

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
            "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": str(event.master_ticket),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if event.expiration:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = event.expiration

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: PLACE %s %s %.2f @ %.5f (ticket=%d, comment=%s)",
                self._name, symbol,
                [k for k, v in self.ORDER_TYPE_MAP.items() if v == order_type][0],
                volume, price, result.order, event.master_ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            comment = result.comment if result else ""
            logger.error(
                "%s: PLACE failed — retcode=%d comment=%s",
                self._name, retcode, comment,
            )
            return False

    def _modify_order(self, symbol: str, event: TradeEvent) -> bool:
        f_order = self._find_order_by_comment(str(event.master_ticket))
        if f_order is None:
            if self._history_has_ticket(str(event.master_ticket)):
                logger.info(
                    "%s: MODIFY_ORDER — ticket %d already done (round trip "
                    "complete), stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: MODIFY_ORDER — no pending order for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        request = {
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": f_order.ticket,
            "symbol": symbol,
            "price": event.price,
            "sl": self._map_price(event.sl, symbol) if event.sl else 0.0,
            "tp": self._map_price(event.tp, symbol) if event.tp else 0.0,
            "magic": self._cfg.magic,
            "comment": f_order.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if event.expiration:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = event.expiration

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: MODIFY_ORDER %s (master_ticket=%d, f_ticket=%d)",
                self._name, symbol, event.master_ticket, f_order.ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            logger.error(
                "%s: MODIFY_ORDER failed — retcode=%d",
                self._name, retcode,
            )
            return False

    def _delete_order(self, symbol: str, event: TradeEvent) -> bool:
        f_order = self._find_order_by_comment(str(event.master_ticket))
        if f_order is None:
            if self._history_has_ticket(str(event.master_ticket)):
                logger.info(
                    "%s: DELETE — ticket %d already done (round trip complete), "
                    "stale event, ignoring",
                    self._name, event.master_ticket,
                )
                return True
            logger.warning(
                "%s: DELETE — no pending order for master ticket %d",
                self._name, event.master_ticket,
            )
            return False

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": f_order.ticket,
            "symbol": symbol,
            "magic": self._cfg.magic,
            "comment": f_order.comment,
        }

        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: DELETE %s (master_ticket=%d, f_ticket=%d)",
                self._name, symbol, event.master_ticket, f_order.ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            logger.error(
                "%s: DELETE failed — retcode=%d",
                self._name, retcode,
            )
            return False

    def _find_order_by_comment(self, comment: str):
        """Find a pending order matching comment AND magic number."""
        orders = mt5.orders_get()
        if orders is None:
            return None
        for order in orders:
            if order.comment == comment and order.magic == self._cfg.magic:
                return order
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _partial_close(
        self, symbol: str, fpos, close_volume: float, event: TradeEvent,
    ) -> bool:
        close_type = mt5.ORDER_TYPE_SELL if fpos.type == 0 else mt5.ORDER_TYPE_BUY
        price = self._get_price(symbol, close_type)
        if price is None:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": fpos.ticket,
            "volume": close_volume,
            "type": close_type,
            "price": price,
            "deviation": self._cfg.deviation,
            "magic": self._cfg.magic,
            "comment": f"pclose_{event.master_ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = self._order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                "%s: PARTIAL CLOSE %s %.2f (master_ticket=%d)",
                self._name, symbol, close_volume, event.master_ticket,
            )
            return True
        else:
            retcode = result.retcode if result else -1
            logger.error(
                "%s: PARTIAL CLOSE failed — retcode=%d",
                self._name, retcode,
            )
            return False

    def _find_position_by_comment(self, comment: str):
        """Find an open position matching comment AND magic number."""
        positions = mt5.positions_get()
        if positions is None:
            return None
        for pos in positions:
            if pos.comment == comment and pos.magic == self._cfg.magic:
                return pos
        return None

    def _history_has_ticket(self, comment: str) -> bool:
        """True if this follower already has deal/order history (7 days) for the
        master ticket (magic + comment match).

        A completed round trip leaves both an entry deal and a close deal in
        MT5 history; a placed pending order leaves its order record even after
        execution or deletion. A history hit therefore means the event was
        materialized in an earlier delivery — used to turn stale re-deliveries
        (hub replay after reconnect, crash-surviving queue entries) into silent
        no-ops instead of re-executing positions the master has long closed.
        """
        try:
            start = datetime.now() - timedelta(days=7)
            end = datetime.now() + timedelta(seconds=1)
            deals = mt5.history_deals_get(start, end)
            if deals:
                for d in deals:
                    if d.comment == comment and d.magic == self._cfg.magic:
                        return True
            orders = mt5.history_orders_get(start, end)
            if orders:
                for o in orders:
                    if o.comment == comment and o.magic == self._cfg.magic:
                        return True
        except Exception as e:
            logger.warning(
                "%s: history dedup check failed (proceeding): %s", self._name, e,
            )
        return False

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

    def _get_price(self, symbol: str, order_type: int) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            # Symbol not quoted yet (e.g. freshly switched account, or the
            # symbol is missing from Market Watch). Request a Market Watch
            # subscription and give the feed a moment to populate before
            # deciding there is no price.
            try:
                selected = mt5.symbol_select(symbol, True)
            except Exception as e:
                selected = False
                logger.warning("%s: symbol_select(%s) raised: %s", self._name, symbol, e)
            logger.info(
                "%s: no tick for %s, symbol_select -> %s, waiting for quote...",
                self._name, symbol, selected,
            )
            for _ in range(6):  # up to ~3s
                time.sleep(0.5)
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None:
                    break
        if tick is None:
            logger.error("%s: no tick for %s", self._name, symbol)
            return None
        return tick.ask if order_type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT) else tick.bid

    def _order_send(self, request: dict):
        """Wrap mt5.order_send with DRY_RUN guard.

        When dry_run is True, logs the request and returns a mock
        result with TRADE_RETCODE_DONE (without executing).
        """
        if self._dry_run:
            logger.info(
                "%s: DRY_RUN would send: %s",
                self._name, self._mask_request(request),
            )
            # Return a mock successful order result
            return type(
                "MockResult", (),
                {"retcode": mt5.TRADE_RETCODE_DONE, "order": 0},
            )()
        return mt5.order_send(request)

    def _mask_request(self, req: dict) -> dict:
        """Remove sensitive fields for logging."""
        masked = dict(req)
        masked.pop("password", None)
        return masked

    # ------------------------------------------------------------------
    # File-based execution (Exness custom builds — no IPC)
    # ------------------------------------------------------------------

    def _pending_path(self) -> str:
        return os.path.join(self._file_data_path, "pending.txt")

    def _result_path(self) -> str:
        return os.path.join(self._file_data_path, "result.txt")

    def _file_build_command(self, action: str, event: TradeEvent) -> str:
        """Build the pipe-delimited command line for TradeReceiver.mq5.

        Market commands use ACTION|SYMBOL|VOLUME|SL|TP|TICKET; pending-order
        commands carry extra fields (order type, price, expiration) and
        DELETE_ORDER needs only the master ticket.
        """
        symbol = self._map_symbol(event.symbol)
        volume = self._apply_lot_scaling(event.volume, symbol)
        ticket = event.master_ticket

        if action in ("PLACE_ORDER", "MODIFY_ORDER"):
            # ACTION|SYMBOL|OTYPE|VOLUME|PRICE|SL|TP|EXPIRATION|TICKET
            otype = event.order_type if event.order_type is not None else event.position_type
            price = f"{event.price:.5f}" if event.price else "0"
            sl_str = f"{event.sl:.5f}" if event.sl else ""
            tp_str = f"{event.tp:.5f}" if event.tp else ""
            exp = str(int(event.expiration)) if event.expiration else "0"
            return f"{action}|{symbol}|{otype}|{volume:.2f}|{price}|{sl_str}|{tp_str}|{exp}|{ticket}"
        if action == "DELETE_ORDER":
            return f"{action}|{ticket}"
        sl_str = f"{event.sl:.5f}" if event.sl else ""
        tp_str = f"{event.tp:.5f}" if event.tp else ""
        return f"{action}|{symbol}|{volume:.2f}|{sl_str}|{tp_str}|{ticket}"

    def _file_send_command(self, action: str, event: TradeEvent) -> Optional[str]:
        """Write a trade command to pending.txt, poll for result.txt.

        Returns the result string (e.g. "DONE|123456") or None on timeout.
        """
        cmd = self._file_build_command(action, event)

        pp = self._pending_path()
        rp = self._result_path()
        tmp = pp + ".tmp"

        # Clean any stale pending and temp files
        for f in (pp, tmp):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass

        # Clean any stale result file
        try:
            if os.path.exists(rp):
                os.remove(rp)
        except OSError:
            pass

        # Write pending command atomically: temp file then rename
        try:
            with open(tmp, "x", encoding="ascii") as f:
                f.write(cmd)
            os.replace(tmp, pp)
        except OSError as e:
            logger.error("%s: failed to write pending file: %s", self._name, e)
            return None

        logger.info(
            "%s: wrote pending command: %s", self._name, cmd,
        )

        # Poll for result (up to 30 seconds)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if os.path.exists(rp):
                try:
                    with open(rp, "r", encoding="ascii") as f:
                        result = f.read().strip()
                    os.remove(rp)
                    return result
                except OSError as e:
                    logger.warning("%s: error reading result: %s", self._name, e)
                    time.sleep(0.5)
                    continue
            time.sleep(0.3)

        logger.warning("%s: timeout waiting for result after 30s", self._name)
        return None

    def _file_execute_event(self, event: TradeEvent) -> bool:
        """Execute a trade event via file relay."""
        if self._dry_run:
            logger.info(
                "%s: DRY_RUN file would send action=%s symbol=%s volume=%.2f",
                self._name, event.action, event.symbol, event.volume,
            )
            return True
        if event.action == "open":
            cmd = "OPEN_BUY" if event.position_type == 0 else "OPEN_SELL"
        elif event.action == "close":
            cmd = "CLOSE"
        elif event.action == "modify":
            # TradeReceiver.mq5 supports MODIFY (TRADE_ACTION_SLTP on the
            # position found by its copied_<ticket> comment). SL/TP are sent
            # in the same command slots as OPEN_BUY.
            cmd = "MODIFY"
        elif event.action == "place":
            cmd = "PLACE_ORDER"
        elif event.action == "modify_order":
            cmd = "MODIFY_ORDER"
        elif event.action == "delete":
            cmd = "DELETE_ORDER"
        elif event.action == "close_all":
            cmd = "CLOSE_ALL"
        elif event.action == "ping":
            cmd = "PING"
        else:
            logger.error("%s: unknown action %s", self._name, event.action)
            return False

        result = self._file_send_command(cmd, event)
        if result is None:
            logger.error("%s: file command timed out for %s", self._name, event.action)
            return False

        if result.startswith("DONE"):
            parts = result.split("|")
            ticket = parts[1] if len(parts) > 1 else "0"
            logger.info(
                "%s: %s (file) -> DONE ticket=%s",
                self._name, event.action.upper(), ticket,
            )
            return True
        if result.startswith("FAILED|NF"):
            # Not found — the follower has nothing matching this ticket (e.g.
            # the bridge was down when the open/place was broadcast, or a
            # previous attempt already succeeded). The desired end state
            # (nothing left to close/modify/delete) already holds, so this is
            # benign — log at info, not error, and do NOT enqueue a retry.
            logger.info(
                "%s: %s (file) -> %s — already consistent, nothing to do",
                self._name, event.action.upper(), result,
            )
            return True
        logger.error(
            "%s: %s (file) -> %s", self._name, event.action.upper(), result,
        )
        return False

    def _file_get_status(self) -> dict:
        """Return placeholder status for file-based mode (no IPC)."""
        # Send PING to verify EA is alive. Use a configured symbol (first
        # symbol_mapping value) instead of a hard-coded one; PING ignores it,
        # but the relay file stays valid for the mapped account.
        ping_symbol = next(iter(self._cfg.symbol_mapping.values()), "XAUUSDc")
        ping_event = TradeEvent(
            action="ping", symbol=ping_symbol, volume=0.01,
            price=0.0, sl=None, tp=None,
            master_ticket=0, position_type=0,
            comment="", magic=0,
        )
        alive = self._file_send_command("PING", ping_event)

        return {
            "name": self._name,
            "active": True,
            "connected": True,
            "trade_allowed": True,
            "file_based": True,
            "account_login": self._cfg.login,
            "server": self._cfg.server,
            "balance": 0,
            "equity": 0,
            "positions": [],
            "position_count": 0,
            "ea_alive": alive is not None and "DONE|PONG" in alive,
        }

    # ------------------------------------------------------------------
    # Local trade event queue (disk-persisted)
    # ------------------------------------------------------------------

    def _load_queue(self) -> list[dict]:
        """Load queued events from disk."""
        with self._queue_lock:
            if not os.path.exists(self._cfg.queue_path):
                return []
            try:
                with open(self._cfg.queue_path, "r") as f:
                    data = json.load(f)
                # Guard against a corrupted queue file (json.load returning a
                # non-list) — reset to an empty queue.
                if not isinstance(data, list):
                    logger.warning(
                        "%s: queue file %s is corrupted (not a list), resetting to empty",
                        self._name, self._cfg.queue_path,
                    )
                    return []
                return data
            except Exception as e:
                logger.warning(
                    "%s: failed to load queue from %s: %s",
                    self._name, self._cfg.queue_path, e,
                )
                return []

    def _save_queue(self, queue: list[dict]) -> None:
        """Save queued events to disk atomically (tmp file + rename)."""
        with self._queue_lock:
            tmp_path = self._cfg.queue_path + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp_path, self._cfg.queue_path)
            except Exception as e:
                logger.warning(
                    "%s: failed to save queue to %s: %s",
                    self._name, self._cfg.queue_path, e,
                )

    def _enqueue_event(self, event: TradeEvent) -> None:
        """Persist a trade event to the local disk queue for later replay."""
        with self._queue_lock:
            import dataclasses
            queue = self._load_queue()
            entry = {
                "event": dataclasses.asdict(event),
                "timestamp": time.time(),
                "retry_count": 0,
            }
            queue.append(entry)
            self._save_queue(queue)
            logger.info(
                "%s: event queued (action=%s ticket=%d, queue size=%d)",
                self._name, event.action, event.master_ticket, len(queue),
            )

    def _dequeue_and_replay(self) -> None:
        """Replay all queued events, dropping entries that exceed retry limit."""
        with self._queue_lock:
            queue = self._load_queue()
            if not queue:
                return
            logger.info(
                "%s: replaying %d queued events...", self._name, len(queue),
            )

            def _entry_key(e: dict) -> tuple:
                # Stable key for dedupe: the event payload (flat primitive dict).
                return tuple(sorted(e.get("event", {}).items()))

            remaining: list[dict] = []
            for entry in queue:
                try:
                    event_dict = entry["event"]
                    # Reconstruct TradeEvent from dict
                    event = TradeEvent(**event_dict)
                    # execute() may persist a fresh copy via _enqueue_event
                    # (connect failure, risk limits, open/place failure). Track
                    # it so the final save below keeps that copy instead of a
                    # stale original overwriting it.
                    queue_len_before = len(self._load_queue())
                    if self.execute(event):
                        logger.info(
                            "%s: replayed queued event for ticket %d",
                            self._name, event.master_ticket,
                        )
                    elif len(self._load_queue()) > queue_len_before:
                        logger.warning(
                            "%s: queued event ticket=%d failed and re-enqueued "
                            "itself, keeping fresh copy",
                            self._name, event.master_ticket,
                        )
                    else:
                        entry["retry_count"] = entry.get("retry_count", 0) + 1
                        if entry["retry_count"] < 3:
                            remaining.append(entry)
                            logger.warning(
                                "%s: queued event ticket=%d failed, %d/3 retries",
                                self._name, event.master_ticket,
                                entry["retry_count"],
                            )
                        else:
                            logger.warning(
                                "%s: dropping queued event ticket=%d after %d failed retries",
                                self._name, event.master_ticket,
                                entry["retry_count"],
                            )
                except Exception as e:
                    logger.warning(
                        "%s: error replaying queued event: %s", self._name, e,
                    )
                    # Keep retrying on deserialization errors (up to 3)
                    entry["retry_count"] = entry.get("retry_count", 0) + 1
                    if entry["retry_count"] < 3:
                        remaining.append(entry)

            # Preserve fresh copies that execute() re-enqueued during replay
            # (they were appended to the on-disk queue after our initial load);
            # _save_queue(remaining) alone would overwrite and lose them.
            fresh = self._load_queue()[len(queue):]
            fresh_keys = {_entry_key(e) for e in fresh}
            for entry in remaining:
                if _entry_key(entry) not in fresh_keys:
                    fresh.append(entry)
                    fresh_keys.add(_entry_key(entry))
            self._save_queue(fresh)
            if fresh:
                logger.info(
                    "%s: %d events still queued after replay",
                    self._name, len(fresh),
                )
