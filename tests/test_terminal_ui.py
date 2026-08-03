"""Unit tests for src/terminal_ui — Windows UI automation helpers.

The real window enumeration / message posting needs a live desktop, so all
tests mock the Windows API (wmic, subprocess, user32) and verify the pure
logic around it: PID matching, kill invocation, and WM_COMMAND posting.
"""

from __future__ import annotations

import unittest
from unittest import mock

from src import terminal_ui


class FindPidsTest(unittest.TestCase):
    def test_wmic_failure_returns_empty(self):
        with mock.patch("subprocess.check_output", side_effect=Exception("no wmic")):
            self.assertEqual(terminal_ui.find_terminal_pids(r"C:\x\terminal64.exe"), [])

    def test_parses_csv_and_matches_path(self):
        out = (
            "Node,ExecutablePath,ProcessId\r\n"
            "PC,C:\\other\\terminal64.exe,111\r\n"
            "PC,C:\\x\\terminal64.exe,222\r\n"
        )
        with mock.patch("subprocess.check_output", return_value=out.encode()):
            pids = terminal_ui.find_terminal_pids(r"c:\X\terminal64.exe")
        self.assertEqual(pids, [222])

    def test_find_first_pid(self):
        with mock.patch("src.terminal_ui.find_terminal_pids", return_value=[11, 22]):
            self.assertEqual(terminal_ui.find_terminal_pid(r"C:\x\terminal64.exe"), 11)

    def test_find_pid_none(self):
        with mock.patch("src.terminal_ui.find_terminal_pids", return_value=[]):
            self.assertIsNone(terminal_ui.find_terminal_pid(r"C:\x\terminal64.exe"))


class KillTerminalTest(unittest.TestCase):
    def test_invokes_taskkill(self):
        with mock.patch("subprocess.call") as call:
            terminal_ui.kill_terminal(123)
        call.assert_called_once()
        cmd = call.call_args.args[0]
        self.assertEqual(cmd, ['taskkill', '/F', '/PID', '123'])

    def test_failure_silent(self):
        with mock.patch("subprocess.call", side_effect=Exception("boom")):
            terminal_ui.kill_terminal(1)  # must not raise


class ToggleAlgoTest(unittest.TestCase):
    def test_posts_wm_command(self):
        with mock.patch("src.terminal_ui._user32.PostMessageW", return_value=1) as pm:
            ok = terminal_ui.toggle_algo_via_wm_command(99)
        self.assertTrue(ok)
        self.assertGreater(pm.call_count, 0)

    def test_no_post_returns_false(self):
        with mock.patch("src.terminal_ui._user32.PostMessageW", return_value=0) as pm:
            ok = terminal_ui.toggle_algo_via_wm_command(99)
        self.assertFalse(ok)
        self.assertGreater(pm.call_count, 0)


class ConstantsTest(unittest.TestCase):
    def test_algo_cmd_ids_known(self):
        self.assertEqual(terminal_ui._ALGO_CMD_IDS, (33051, 33050, 33052, 32808))


if __name__ == "__main__":
    unittest.main()
