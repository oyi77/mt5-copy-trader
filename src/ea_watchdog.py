"""EA-mode master watchdog — auto-recover TradeSender after terminal loss.

``TradeSender.mq5`` runs only while the master terminal is up AND the EA is
attached to a chart. A hard kill of the terminal (or a crash) therefore leaves
the signal file stale: the bridge marks ``master_connected = False`` but
nothing brings the EA back on its own.

This module implements a careful recovery sequence:

1. Gracefully close the master terminal (``CloseMainWindow``). MT5 saves its
   chart profile on a normal close, so the EA re-attaches automatically on
   the next start.
2. If the process does not exit within a few seconds, force-kill it — but
   ONLY when its executable path equals ``master.path``. Other terminals
   (e.g. a live-trading install) are never touched.
3. Relaunch the terminal (with optional ``/login:login,password,server``
   credentials).
4. Wait for the signal-file heartbeat to resume.
5. If it does not resume (the chart profile did not restore the EA), run the
   configured re-attach script (UI automation, e.g. ``attach_ea.ps1``) and
   wait again.

Recovery is rate-limited (one attempt per ``retry_interval`` seconds, at most
``max_attempts`` before giving up until the bridge restarts), so a broken
setup cannot churn the terminal process.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Callable that waits up to `timeout` seconds for the EA to be reachable
# again and returns True when it is.
AliveCheck = Callable[[float], bool]


def _ps_quote(path: str) -> str:
    """Escape a path for embedding inside a PowerShell single-quoted string."""
    return path.replace("'", "''")


class EaWatchdog:
    """Rate-limited auto-recovery for the EA-mode master terminal."""

    def __init__(
        self,
        terminal_exe: str,
        attach_script: str = "",
        login: int = 0,
        password: str = "",
        server: str = "",
        *,
        retry_interval: float = 300.0,
        max_attempts: int = 3,
        graceful_wait: float = 15.0,
        alive_wait: float = 45.0,
    ) -> None:
        # Get-Process.Path returns canonical backslash paths; configs often
        # carry forward-slash paths (e.g. from YAML), so normalize once here.
        self._exe = os.path.normcase(os.path.normpath(terminal_exe))
        self._attach_script = attach_script
        self._login_args = ""
        if login and password and server:
            self._login_args = f"/login:{login},{password},{server}"
        self._retry_interval = retry_interval
        self._max_attempts = max_attempts
        self._graceful_wait = graceful_wait
        self._alive_wait = alive_wait
        self._attempts = 0
        self._next_attempt_at = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_attempt(self) -> bool:
        """True when a recovery attempt is allowed (cooldown + budget)."""
        if self._attempts >= self._max_attempts:
            return False
        return time.monotonic() >= self._next_attempt_at

    def attempt_recovery(self, wait_alive: AliveCheck) -> str:
        """Run one full recovery cycle; return a short human-readable status.

        ``wait_alive`` blocks up to its timeout while polling for the EA
        heartbeat and returns True when the EA is reachable again.
        """
        if not self.can_attempt():
            return "skipped (cooldown or attempt budget exhausted)"
        self._attempts += 1
        self._next_attempt_at = time.monotonic() + self._retry_interval
        logger.warning(
            "EA watchdog: recovery attempt %d/%d (terminal %s)",
            self._attempts, self._max_attempts, self._exe,
        )

        # 1+2. Graceful close, then force-kill if it hangs.
        pid = self._find_terminal_pid()
        if pid is not None:
            self._close_terminal(pid)
            if self._wait_process_exit(pid, self._graceful_wait):
                logger.info("EA watchdog: terminal %s exited gracefully", pid)
            else:
                logger.warning("EA watchdog: terminal %s did not exit, force-killing", pid)
                self._kill_terminal(pid)
        else:
            logger.info("EA watchdog: no running master terminal found")

        # 3. Relaunch.
        cmd = [self._exe]
        if self._login_args:
            cmd.append(self._login_args)
        try:
            subprocess.Popen(cmd)
            logger.info("EA watchdog: relaunched %s", self._exe)
        except OSError as e:
            logger.error("EA watchdog: failed to launch %s: %s", self._exe, e)
            return f"failed (launch error: {e})"

        # 4. Wait for the heartbeat.
        if wait_alive(self._alive_wait):
            logger.info("EA watchdog: EA heartbeat resumed after relaunch")
            return "recovered (relaunch restored the EA)"

        # 5. Attach-script fallback (profile did not restore the EA).
        if self._attach_script:
            logger.warning(
                "EA watchdog: heartbeat not resumed, running attach script %s",
                self._attach_script,
            )
            self._run_attach_script()
            if wait_alive(self._alive_wait):
                logger.info("EA watchdog: EA heartbeat resumed after attach script")
                return "recovered (attach script re-attached the EA)"

        logger.error(
            "EA watchdog: recovery failed — re-attach TradeSender manually "
            "(e.g. attach_ea.ps1 -TerminalPath \"%s\")", self._exe,
        )
        return "failed (EA not running; re-attach manually)"

    # ------------------------------------------------------------------
    # Process control (path-filtered — other terminals are never touched)
    # ------------------------------------------------------------------

    def _find_terminal_pid(self) -> Optional[int]:
        """Return the PID of the terminal whose path equals self._exe."""
        quoted = _ps_quote(self._exe)
        script = (
            "$p = Get-Process -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.Path -eq '{quoted}' }} | Select-Object -First 1; "
            "if ($p) { Write-Output $p.Id }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("EA watchdog: process lookup failed: %s", e)
            return None
        line = out.stdout.strip()
        return int(line) if line.isdigit() else None

    def _close_terminal(self, pid: int) -> None:
        """Ask the terminal to close normally (saves the chart profile)."""
        script = (
            f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
            "if ($p) { $null = $p.CloseMainWindow(); Write-Output 'sent' }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("EA watchdog: graceful close failed: %s", e)

    def _kill_terminal(self, pid: int) -> None:
        """Force-kill the terminal process (path-filtered at lookup time)."""
        script = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("EA watchdog: force-kill failed: %s", e)

    def _wait_process_exit(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._find_terminal_pid() is None:
                return True
            time.sleep(1.0)
        return False

    def _run_attach_script(self) -> None:
        """Run the configured re-attach script (UI automation fallback)."""
        script = _ps_quote(self._attach_script)
        exe = _ps_quote(self._exe)
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 self._attach_script, "-TerminalPath", self._exe],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.error("EA watchdog: attach script failed to run: %s", e)
            return
        logger.info(
            "EA watchdog: attach script exit=%d stdout=%.200s stderr=%.200s",
            result.returncode, result.stdout.strip(), result.stderr.strip(),
        )
