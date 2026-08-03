"""Tests for src.ea_watchdog.EaWatchdog — EA-mode master auto-recovery."""

from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from src.ea_watchdog import EaWatchdog

# Constructors below use forward-slash paths on purpose: configs carry them
# (e.g. from YAML) and the watchdog must normalize to the canonical backslash
# form before comparing against Get-Process.Path / launching.
_EXE = os.path.normcase(os.path.normpath("C:/x/terminal64.exe"))


class PathNormalizationTest(unittest.TestCase):
    def test_forward_slash_path_is_normalized(self):
        w = EaWatchdog("C:/x/terminal64.exe")
        self.assertEqual(w._exe, _EXE)
        w2 = EaWatchdog("c:/x/terminal64.exe/")
        self.assertEqual(w2._exe, _EXE)

    def test_attach_script_gets_normalized_terminal_path(self):
        w = EaWatchdog("C:/x/terminal64.exe", attach_script="C:/x/attach_ea.ps1")
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen"), \
             mock.patch("src.ea_watchdog.subprocess.run") as run:
            run.return_value = SimpleNamespace(stdout="", returncode=0, stderr="")
            seq = iter([False, True])  # relaunch wait fails, attach wait succeeds
            status = w.attempt_recovery(lambda t: next(seq))
        self.assertEqual(status, "recovered (attach script re-attached the EA)")
        commands = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any("-File" in cmd and "C:/x/attach_ea.ps1" in cmd
                            and "-TerminalPath" in cmd for cmd in commands))
        self.assertTrue(any(_EXE in cmd for cmd in commands))


class CooldownAndBudgetTest(unittest.TestCase):
    def test_cooldown_blocks_early_attempts(self):
        w = EaWatchdog("C:/x/terminal64.exe", retry_interval=300.0, max_attempts=3)
        self.assertTrue(w.can_attempt())
        w._next_attempt_at = time.monotonic() + 1000.0
        self.assertFalse(w.can_attempt())
        w._next_attempt_at = 0.0
        self.assertTrue(w.can_attempt())

    def test_attempt_budget_gives_up(self):
        w = EaWatchdog("C:/x/terminal64.exe", retry_interval=0.0, max_attempts=1)
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen") as popen:
            status = w.attempt_recovery(lambda t: True)
        self.assertEqual(status, "recovered (relaunch restored the EA)")
        popen.assert_called_once_with([_EXE])
        # Budget exhausted — a second call must not touch the process again.
        self.assertFalse(w.can_attempt())
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen") as popen2:
            w.attempt_recovery(lambda t: True)
        popen2.assert_not_called()


class RecoveryFlowTest(unittest.TestCase):
    def test_no_process_relaunch_and_heartbeat_resume(self):
        w = EaWatchdog("C:/x/terminal64.exe")
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen") as popen:
            status = w.attempt_recovery(lambda t: True)
        self.assertEqual(status, "recovered (relaunch restored the EA)")
        popen.assert_called_once_with([_EXE])

    def test_relaunch_with_login_credentials(self):
        w = EaWatchdog("C:/x/terminal64.exe", login=1, password="pw", server="srv")
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen") as popen:
            w.attempt_recovery(lambda t: True)
        popen.assert_called_once_with([_EXE, "/login:1,pw,srv"])

    def test_hung_terminal_is_gracefully_closed_then_force_killed(self):
        w = EaWatchdog("C:/x/terminal64.exe", graceful_wait=1.0)
        with mock.patch.object(w, "_find_terminal_pid", return_value=1234), \
             mock.patch.object(w, "_wait_process_exit", return_value=False), \
             mock.patch("src.ea_watchdog.subprocess.Popen") as popen, \
             mock.patch("src.ea_watchdog.subprocess.run") as run:
            run.return_value = SimpleNamespace(stdout="", returncode=0, stderr="")
            w.attempt_recovery(lambda t: True)
        commands = [c.args[0] for c in run.call_args_list]
        # "CloseMainWindow" / "Stop-Process" live inside the PowerShell script
        # string element of each args list — check for substring membership.
        self.assertTrue(any(
            any("CloseMainWindow" in s for s in cmd) for cmd in commands))
        self.assertTrue(any(
            any("Stop-Process" in s for s in cmd) for cmd in commands))
        popen.assert_called_once_with([_EXE])

    def test_attach_script_fallback_when_heartbeat_does_not_resume(self):
        w = EaWatchdog("C:/x/terminal64.exe", attach_script="C:/x/attach_ea.ps1")
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen"), \
             mock.patch("src.ea_watchdog.subprocess.run") as run:
            run.return_value = SimpleNamespace(stdout="", returncode=0, stderr="")
            seq = iter([False, True])  # relaunch wait fails, attach wait succeeds
            status = w.attempt_recovery(lambda t: next(seq))
        self.assertEqual(status, "recovered (attach script re-attached the EA)")
        commands = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any("-File" in cmd and "C:/x/attach_ea.ps1" in cmd
                            and "-TerminalPath" in cmd for cmd in commands))

    def test_failure_reports_manual_reattach(self):
        w = EaWatchdog("C:/x/terminal64.exe")
        with mock.patch.object(w, "_find_terminal_pid", return_value=None), \
             mock.patch("src.ea_watchdog.subprocess.Popen"):
            status = w.attempt_recovery(lambda t: False)
        self.assertTrue(status.startswith("failed"))


if __name__ == "__main__":
    unittest.main()
