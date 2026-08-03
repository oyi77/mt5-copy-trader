"""Unit tests for install_ea.py (EA file-bridge installer).

Only pure logic is tested here: chart-profile decoding/encoding, expert-block
building + upsert, and data-dir derivation. Compiling via MetaEditor needs a
real terminal install, so that path is left to the live smoke test.
"""

import os
import tempfile
import unittest
from pathlib import Path

import install_ea as iea


class ChrCodecTest(unittest.TestCase):
    def test_round_trip_utf16le(self):
        text = "<chart>\r\nwindows_total=1\r\n</chart>\r\n"
        raw = iea.chr_encode(text, "\r\n")
        self.assertTrue(raw.startswith(b"\xff\xfe"))
        text2, nl = iea.chr_decode(raw)
        self.assertEqual(text, text2)
        self.assertEqual(nl, "\r\n")

    def test_detects_lf_profiles(self):
        raw = b"\xff\xfe" + "<chart>\n</chart>".encode("utf-16-le")
        text, nl = iea.chr_decode(raw)
        self.assertEqual(nl, "\n")
        self.assertEqual(text, "<chart>\n</chart>")

    def test_rejects_non_bom_encoding(self):
        with self.assertRaises(ValueError):
            iea.chr_decode(b"<chart>plain ascii</chart>")


class BuildBlockTest(unittest.TestCase):
    def test_follower_block(self):
        inputs = [("TimerIntervalMS", "500"), ("ExpertMagic", "200001")]
        block = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", inputs, "\r\n")
        lines = block.split("\r\n")
        self.assertEqual(lines[0], "<expert>")
        self.assertIn("name=TradeReceiver", lines)
        self.assertIn("path=Experts\\TradeReceiver.ex5", lines)
        self.assertEqual(lines[3], "expertmode=5")
        self.assertIn("<inputs>", lines)
        self.assertIn("ExpertMagic=200001", lines)
        self.assertEqual(lines[-1], "</expert>")


CHART = (
    "<chart>\r\n"
    "id=1\r\n"
    "symbol=EURUSD\r\n"
    "windows_total=1\r\n"
    "\r\n"
    "<window>\r\n"
    "name=Main\r\n"
    "</window>\r\n"
    "</chart>\r\n"
)


class UpsertTest(unittest.TestCase):
    def test_inserts_after_windows_total(self):
        block = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", [("ExpertMagic", "200001")], "\r\n")
        out = iea.upsert_expert_block(CHART, block, "\r\n")
        lines = out.split("\r\n")
        wt = lines.index("windows_total=1")
        self.assertEqual(lines[wt + 1], "")  # blank separator
        self.assertEqual(lines[wt + 2], "<expert>")
        self.assertIn("name=TradeReceiver", lines)
        # The window content must still follow the block, inside the chart.
        self.assertLess(lines.index("<window>"), lines.index("</chart>"))
        self.assertLess(lines.index("</expert>"), lines.index("<window>"))

    def test_replaces_existing_same_name(self):
        block = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", [("ExpertMagic", "123")], "\r\n")
        first = iea.upsert_expert_block(CHART, block, "\r\n")
        # Re-run with a different magic: block is replaced, not duplicated.
        block2 = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", [("ExpertMagic", "456")], "\r\n")
        out = iea.upsert_expert_block(first, block2, "\r\n")
        self.assertEqual(out.count("<expert>"), 1)
        self.assertIn("ExpertMagic=456", out)
        self.assertNotIn("ExpertMagic=123", out)

    def test_idempotent_identical_block(self):
        block = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", [("ExpertMagic", "200001")], "\r\n")
        once = iea.upsert_expert_block(CHART, block, "\r\n")
        twice = iea.upsert_expert_block(once, block, "\r\n")
        self.assertEqual(once, twice)  # no blank-line accumulation

    def test_keeps_other_experts(self):
        other = iea.build_expert_block("SomeOtherEA", "SomeOtherEA.ex5", [], "\r\n")
        chart = iea.upsert_expert_block(CHART, other, "\r\n")
        block = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", [], "\r\n")
        out = iea.upsert_expert_block(chart, block, "\r\n")
        self.assertEqual(out.count("<expert>"), 2)
        self.assertIn("name=SomeOtherEA", out)
        self.assertIn("name=TradeReceiver", out)

    def test_falls_back_before_chart_close(self):
        block = iea.build_expert_block("TradeReceiver", "TradeReceiver.ex5", [], "\r\n")
        bare = "<chart>\r\n</chart>\r\n"
        out = iea.upsert_expert_block(bare, block, "\r\n")
        self.assertIn("<expert>", out)
        self.assertLess(out.index("<expert>"), out.index("</chart>"))


class ConfigDeriveTest(unittest.TestCase):
    def _write(self, yaml_text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        return path

    def test_master_from_signals_file(self):
        cfg = self._write(
            "master:\n  ea_signals_file: C:/x/Terminal/ABC/MQL5/Files/master_signals.txt\n"
        )
        self.assertEqual(Path(iea._config_data_dir("master", cfg)), Path("C:/x/Terminal/ABC"))

    def test_follower_from_terminal_data_path(self):
        cfg = self._write(
            "followers:\n  - name: f1\n    terminal_data_path: C:/x/Terminal/ABC/MQL5/Files\n"
        )
        self.assertEqual(Path(iea._config_data_dir("follower", cfg)), Path("C:/x/Terminal/ABC"))

    def test_master_missing_key_raises(self):
        with self.assertRaises(SystemExit):
            iea._config_data_dir("master", self._write("master: {}\n"))

    def test_follower_missing_key_raises(self):
        with self.assertRaises(SystemExit):
            iea._config_data_dir("follower", self._write("followers: []\n"))


class EndToEndFileTest(unittest.TestCase):
    def test_full_attach_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            chart = Path(td) / "chart01.chr"
            chart.write_bytes(iea.chr_encode(CHART, "\r\n"))
            raw = chart.read_bytes()
            text, nl = iea.chr_decode(raw)
            block = iea.build_expert_block(
                "TradeReceiver", "TradeReceiver.ex5",
                [("TimerIntervalMS", "500"), ("ExpertMagic", "200001")], nl,
            )
            chart.write_bytes(iea.chr_encode(iea.upsert_expert_block(text, block, nl), nl))
            # Re-open the exact file the terminal would load.
            text2, _ = iea.chr_decode(chart.read_bytes())
            self.assertIn("name=TradeReceiver", text2)
            self.assertIn("TimerIntervalMS=500", text2)
            self.assertLess(text2.index("windows_total=1"), text2.index("<expert>"))


if __name__ == "__main__":
    unittest.main()
