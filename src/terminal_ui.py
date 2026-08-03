"""Windows UI automation for the MT5 terminals — no trading logic.

Everything in this module talks to the Windows GUI layer of a MetaTrader 5
terminal: enumerating windows, dismissing modal dialogs, and toggling the
Algo-trading button (which cannot be enabled from config files — it needs a
visible window and a Ctrl+E / WM_COMMAND push).

Kept separate from the trading engine (src/follower.py) so the executor
stays focused on trade execution, risk limits and the file relay, and so
these ctypes/PowerShell helpers can be unit-tested and reused by other
terminal-management code (e.g. ea_watchdog) without dragging trading state
along.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

# Windows API user32.dll — used by all window automation below.
_user32 = ctypes.WinDLL('user32', use_last_error=True)

_WM_COMMAND = 0x0111
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_VK_ESCAPE = 27
_VK_RETURN = 13
_ALGO_CMD_IDS = (33051, 33050, 33052, 32808)  # known MT5 Algo command IDs


def find_terminal_pids(exe_path: str) -> list:
    """Return list of PIDs of terminal64.exe processes whose path matches.

    Uses WMIC without shell=True for reliable execution. The path comparison
    is a normalized substring match, so it also catches the same exe reached
    through a different casing or a symlink.
    """
    pids: list = []
    our_path = exe_path.replace('/', '\\').lower()
    try:
        out = subprocess.check_output(
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
                exe_path_c = parts[-2].strip()
                pid_str = parts[-1].strip()
                if exe_path_c and pid_str and pid_str.isdigit():
                    if our_path in exe_path_c.lower():
                        pids.append(int(pid_str))
    except Exception as e:
        logger.debug("wmic lookup failed: %s", e)
    return pids


def find_terminal_pid(exe_path: str) -> Optional[int]:
    """Return the first PID of a running terminal64.exe matching exe_path."""
    pids = find_terminal_pids(exe_path)
    if pids:
        logger.debug("found terminal PID %s at %s", pids[0], exe_path)
        return pids[0]
    return None


def kill_terminal(pid: int) -> None:
    """Force-kill a terminal process by PID (best effort)."""
    try:
        subprocess.call(
            ['taskkill', '/F', '/PID', str(pid)],
            timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning("taskkill %d failed: %s", pid, e)


def wait_for_terminal_window(exe_path: str, name: str, timeout: float = 20.0) -> Optional[int]:
    """Wait for any visible top-level window belonging to terminal64.exe.

    More robust than FindWindowW by class name — enumerates all top-level
    windows and checks each one's owning process via GetWindowThreadProcessId.
    Returns the MetaTrader main-frame hwnd, or None on timeout.
    """
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _GetWindowThreadProcessId.restype = wintypes.DWORD
    _IsWindowVisible = _user32.IsWindowVisible
    _IsWindowVisible.argtypes = [wintypes.HWND]
    _IsWindowVisible.restype = wintypes.BOOL

    # Callback for EnumWindows — uses outer-scope pids/hwnd variables
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    _pid_buf = wintypes.DWORD()
    _title_buf = ctypes.create_unicode_buffer(256)
    _class_buf = ctypes.create_unicode_buffer(256)
    _GetClassNameW = _user32.GetClassNameW
    _GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _GetClassNameW.restype = ctypes.c_int

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        terminal_pids = find_terminal_pids(exe_path)
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
                    name, hwnd, title, cls,
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
                name, found_metatrader[0],
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
                name, found_any[0],
            )
            return found_any[0]

        time.sleep(0.5)

    return None


def dismiss_blocking_dialogs(name: str, main_hwnd: int) -> None:
    """Close any visible modal dialogs (Login, connection prompts) that
    belong to the same process as main_hwnd and might block input.

    Uses multiple approaches:
    - WM_CLOSE (standard close)
    - WM_COMMAND(IDCANCEL=2) (dialog cancel button)
    - WM_DESTROY (forceful destroy)
    - Simulated Escape key (WM_KEYDOWN/WM_KEYUP)
    - Simulated Enter key (to press default OK button)
    """
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _GetWindowThreadProcessId.restype = wintypes.DWORD

    # Get the PID of the main window
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(main_hwnd, ctypes.byref(pid))
    target_pid = pid.value

    if not target_pid:
        return

    _PostMessageW = _user32.PostMessageW
    _PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _PostMessageW.restype = wintypes.BOOL
    _GetWindowTextW = _user32.GetWindowTextW
    _GetClassNameW = _user32.GetClassNameW
    _IsWindowVisible = _user32.IsWindowVisible
    _title_buf = ctypes.create_unicode_buffer(256)
    _class_buf = ctypes.create_unicode_buffer(256)
    _pid_buf = wintypes.DWORD()

    _target_pid_ref = [target_pid]
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

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


def toggle_algo_via_wm_command(hwnd: int) -> bool:
    """Toggle the Algo button by sending WM_COMMAND directly.

    Sends WM_COMMAND messages with known Algo button command IDs
    directly to the main MetaTrader frame window via PostMessageW.
    This bypasses keyboard/focus/foreground issues — it works even
    when the window is not in the foreground.
    """
    _PostMessageW = _user32.PostMessageW
    _PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _PostMessageW.restype = wintypes.BOOL

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


def send_ctrl_e_via_powershell(pid: int, hwnd: int = 0) -> None:
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
    try:
        result = subprocess.check_output(
            ['powershell', '-STA', '-ExecutionPolicy', 'Bypass',
             '-Command', ps_script],
            timeout=15, stderr=subprocess.STDOUT,
        )
        logger.info(
            "PowerShell SendKeys result: %s",
            result.decode('utf-8', errors='replace').strip(),
        )
    except Exception as e:
        logger.warning("PowerShell SendKeys failed: %s", e)
