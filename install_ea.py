"""One-command installer for the EA file-bridge (TradeSender / TradeReceiver).

The EA file-bridge needs three pieces before it can run: the EA source
compiled to .ex5, that .ex5 deployed into the terminal's data dir
(MQL5\\Experts\\), and the EA attached to a chart. This script automates all
three — no clicking around the terminal UI.

  # Follower side (executes commands via TradeReceiver.mq5)
  python install_ea.py --role follower --data-dir C:/Users/me/AppData/Roaming/MetaQuotes/Terminal/<hash>

  # Master side (emits signals via TradeSender.mq5)
  python install_ea.py --role master --data-dir C:/Users/me/AppData/Roaming/MetaQuotes/Terminal/<hash>

  # Derive the data dir from an existing config file instead
  python install_ea.py --role follower --config agent_config.yaml
  python install_ea.py --role master   --config config.yaml

The terminal data dir is the folder that contains MQL5/ (e.g.
...\\Terminal\\<hash>). For the master it is where
MQL5\\Files\\master_signals.txt lives; for a follower it is the parent of the
agent's configured terminal_data_path (which is the MQL5\\Files folder).

Attaching writes the EA into the chart profile <data>\\MQL5\\Profiles\\Charts\\DEFAULT\\chartNN.chr
(UTF-16LE, the format the terminal itself uses), so the terminal attaches the
EA at startup with no UI automation. By default the prebuilt .ex5 kept beside
this script is deployed (no compiler needed); pass --compile to rebuild from
source with MetaEditor instead. Remember the active-tab rule: the EA's timer
only fires while its chart is the active/visible tab, so attach it to the
chart that is in the foreground (chart 1 by default).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Role definitions: name -> (source .mq5, compiled .ex5, default chart inputs)
EA_ROLES = {
    "master": {
        "ea_name": "TradeSender",
        "source": "TradeSender.mq5",
        "ex5": "TradeSender.ex5",
        "default_inputs": [
            ("PollIntervalMS", "500"),
            ("HeartbeatIntervalMS", "10000"),
            ("SignalFile", "master_signals.txt"),
            ("MaxFileBytes", "5242880"),
        ],
    },
    "follower": {
        "ea_name": "TradeReceiver",
        "source": "TradeReceiver.mq5",
        "ex5": "TradeReceiver.ex5",
        "default_inputs": [
            ("TimerIntervalMS", "500"),
            ("MaxSlippage", "50"),
            ("ExpertMagic", "200001"),
            ("PendingFile", "pending.txt"),
            ("ResultFile", "result.txt"),
        ],
    },
}

CHART_RE = re.compile(r"^chart(\d+)\.chr$", re.IGNORECASE)
RESULT_RE = re.compile(r"Result:\s*(\d+)\s*errors")


# ── Chart profile (.chr) helpers ─────────────────────────────────────────

def chr_decode(data: bytes) -> tuple[str, str]:
    """Decode a .chr file to (text, line_ending).

    MT5 writes chart profiles as UTF-16LE with a BOM. Other encodings are
    refused rather than guessed at — corrupting a profile is worse than
    asking the user to re-save it from the terminal.
    """
    if data.startswith(b"\xff\xfe"):
        text = data[2:].decode("utf-16-le")
        return text, "\r\n" if "\r\n" in text else "\n"
    if data.startswith(b"\xfe\xff"):
        text = data[2:].decode("utf-16-be")
        return text, "\r\n" if "\r\n" in text else "\n"
    raise ValueError(
        "unexpected .chr encoding (first bytes %r) — expected UTF-16 with BOM; "
        "open the chart profile in the terminal and re-save it, or pass a "
        "fresh profile" % data[:4]
    )


def chr_encode(text: str, nl: str) -> bytes:
    """Encode text back to the UTF-16LE+BOM .chr format."""
    return b"\xff\xfe" + text.replace("\r\n", "\n").replace("\n", nl).encode("utf-16-le")


def build_expert_block(ea_name: str, ex5: str, inputs: list[tuple[str, str]], nl: str) -> str:
    """Build the <expert> block for a chart profile, terminal .chr style."""
    lines = ["<expert>", f"name={ea_name}", f"path=Experts\\{ex5}", "expertmode=5", "<inputs>"]
    lines += [f"{key}={value}" for key, value in inputs]
    lines += ["</inputs>", "</expert>"]
    return nl.join(lines)


def upsert_expert_block(text: str, block: str, nl: str) -> str:
    """Insert (or replace) an expert block in a chart profile's text.

    An existing block with the same ``name=`` is replaced *in place* (its
    surrounding blank lines are untouched), which makes re-runs idempotent.
    When there is no such block yet, one is inserted after the
    ``windows_total=`` line — the spot the terminal itself uses — falling
    back to before the first ``<window>``, then before ``</chart>``.
    """
    lines = text.split(nl)
    block_lines = block.split(nl)
    block_name = block_lines[1]  # "name=<EA>"

    # Replace an existing block with the same EA name in place.
    i = 0
    while i < len(lines):
        if lines[i].strip() == "<expert>":
            j = i
            while j < len(lines) and lines[j].strip() != "</expert>":
                j += 1
            if j < len(lines) and block_name in lines[i : j + 1]:
                lines[i : j + 1] = block_lines
                return nl.join(lines)
        i += 1

    # No existing block: find the insertion index. Prefer after
    # windows_total=; fall back to before <window>, then before </chart>.
    insert_after = next(
        (idx for idx, ln in enumerate(lines) if ln.startswith("windows_total=")),
        None,
    )
    if insert_after is not None:
        before = insert_after + 1
    else:
        before = next(
            (idx for idx, ln in enumerate(lines) if ln.strip() == "<window>"),
            None,
        )
        if before is None:
            before = next(
                (idx for idx, ln in enumerate(lines) if ln.strip() == "</chart>"),
                len(lines),
            )

    # Mirror the terminal's own layout: a blank line around the block.
    lines[before:before] = ["", *block_lines, ""]
    return nl.join(lines)


# ── MetaEditor compile ────────────────────────────────────────────────────

def compile_ea(metaeditor: str, src_mq5: str, out_ex5: str) -> tuple[bool, str]:
    """Compile an .mq5 with MetaEditor64.exe; return (ok, log_text).

    MetaEditor's behavior for a source file located inside a *running or
    known* terminal's data dir is unreliable (it writes a .log but may not
    emit the .ex5). To sidestep that, the source is staged into a neutral
    temp folder, compiled there (guaranteed to emit .ex5.next to the
    source), and only the resulting .ex5 is copied into the deploy target.
    Paths are passed to MetaEditor in native Windows backslash form.
    """
    src = os.path.abspath(src_mq5)
    out = os.path.abspath(out_ex5)
    with tempfile.TemporaryDirectory(prefix="ea_compile_") as td:
        staged = os.path.join(td, os.path.basename(src))
        shutil.copy2(src, staged)
        ex5 = os.path.splitext(staged)[0] + ".ex5"
        log_arg = os.path.join(td, "compile.log").replace("/", os.sep)
        args = [
            metaeditor,
            f'/compile:"{staged.replace("/", os.sep)}"',
            f'/log:"{log_arg}"',
        ]
        logger.info("compiling %s -> %s", Path(src).name, out_ex5)
        proc = subprocess.run(args, capture_output=True, text=True, timeout=300)

        # MetaEditor writes the log next to the source as <name>.log,
        # regardless of the /log argument.
        log_path = os.path.join(td, os.path.splitext(os.path.basename(staged))[0] + ".log")
        log_text = ""
        for candidate in (log_path, log_arg):
            if os.path.exists(candidate):
                raw = Path(candidate).read_bytes()
                try:
                    if raw.startswith(b"\xff\xfe"):
                        log_text = raw[2:].decode("utf-16-le")
                    else:
                        log_text = raw.decode("utf-8", errors="replace")
                    break
                except (UnicodeDecodeError, OSError):
                    continue
        if not log_text:
            log_text = (proc.stdout or "") + (proc.stderr or "") or "(no compile log produced)"

        match = RESULT_RE.search(log_text)
        errors = int(match.group(1)) if match else None
        ok = errors == 0 and os.path.exists(ex5)
        if ok:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(ex5, out)
            logger.info(
                "deployed %s (%d bytes)", Path(out).name, os.path.getsize(out)
            )
        else:
            tail = "\n".join(log_text.splitlines()[-15:])
            logger.error(
                "compile failed%s (%s not produced):\n%s",
                f" ({errors} errors)" if errors is not None else "",
                Path(out).name,
                tail,
            )
        return ok, log_text


# ── Data-dir discovery ────────────────────────────────────────────────────

def _config_data_dir(role: str, config_path: str) -> str:
    """Derive the terminal data dir from a YAML config file."""
    import yaml

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if role == "master":
        signal = (data.get("master") or {}).get("ea_signals_file") or ""
        if signal:
            # <data>\MQL5\Files\master_signals.txt -> <data>
            return str(Path(signal).parent.parent.parent)
        raise SystemExit(
            "config.yaml has no master.ea_signals_file — set it to the "
            "master_signals.txt path first (see README 'EA-only master mode')"
        )

    for follower in data.get("followers") or []:
        tdp = (follower or {}).get("terminal_data_path") or ""
        if tdp:
            # <data>\MQL5\Files -> <data>
            return str(Path(tdp).parent.parent)
    raise SystemExit(
        "agent_config.yaml has no follower with terminal_data_path — set it "
        "to the terminal's MQL5\\Files folder first (see README 'File-relay mode')"
    )


def _repo_metaeditor() -> str:
    """Default MetaEditor64.exe shipped next to the bundled terminal, if any."""
    candidate = Path(__file__).parent / "mt5_exness" / "MetaEditor64.exe"
    return str(candidate) if candidate.exists() else ""


# ── Main ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile, deploy and attach the EA file-bridge (TradeSender / TradeReceiver).",
    )
    parser.add_argument("--role", required=True, choices=["master", "follower"])
    parser.add_argument("--data-dir", help="terminal data dir (the folder containing MQL5/)")
    parser.add_argument(
        "--config",
        help="YAML config file to derive --data-dir from (config.yaml for master, agent_config.yaml for follower)",
    )
    parser.add_argument("--metaeditor", help="path to MetaEditor64.exe (default: repo mt5_exness copy)")
    parser.add_argument("--chart", type=int, default=1, help="chart number to attach the EA to (default 1)")
    parser.add_argument("--magic", type=int, default=200001, help="ExpertMagic for TradeReceiver (default 200001)")
    parser.add_argument("--inputs", action="append", default=[], metavar="KEY=VALUE", help="override an EA input")
    parser.add_argument(
        "--compile",
        action="store_true",
        help="force a fresh MetaEditor compile instead of deploying the bundled .ex5 (best-effort)",
    )
    parser.add_argument("--ex5", help="path to a specific .ex5 to deploy instead of the bundled one")
    parser.add_argument("--no-attach", action="store_true", help="compile + deploy only, do not touch chart profiles")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    role = EA_ROLES[args.role]
    ea_name = role["ea_name"]

    # 1. Resolve the terminal data dir.
    if args.data_dir:
        data_dir = Path(args.data_dir)
    elif args.config:
        data_dir = Path(_config_data_dir(args.role, args.config))
    else:
        parser.error("pass --data-dir or --config")
    files_dir = data_dir / "MQL5" / "Files"
    if not files_dir.is_dir():
        logger.error(
            "%s is not a terminal data dir — expected %s to exist (MQL5\\Files)",
            data_dir, files_dir,
        )
        return 1
    logger.info("terminal data dir: %s", data_dir)

    # 2. Deploy the .ex5. Compiling is best-effort (MetaEditor's headless
    #    /compile can silently no-op on some machines); the default is to
    #    copy the bundled prebuilt .ex5 from this script's folder, which is
    #    deterministic and needs no compiler.
    src_mq5 = Path(__file__).parent / role["source"]
    experts_dir = data_dir / "MQL5" / "Experts"
    if not src_mq5.exists():
        logger.error("EA source not found next to this script: %s", src_mq5)
        return 1
    experts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_mq5, experts_dir / role["source"])  # keep source alongside

    out_ex5 = experts_dir / role["ex5"]
    compiled_ok = False
    if args.compile:
        metaeditor = args.metaeditor or _repo_metaeditor()
        if not metaeditor:
            parser.error(
                "--compile but no MetaEditor64.exe found — pass --metaeditor "
                "<path>, or drop --compile to deploy the bundled .ex5"
            )
        compiled_ok, _ = compile_ea(metaeditor, str(src_mq5), str(out_ex5))
        if compiled_ok:
            logger.info("deployed %s (%d bytes)", out_ex5.name, out_ex5.stat().st_size)

    if not compiled_ok:
        if args.compile:
            logger.warning("MetaEditor compile failed - deploying an existing/prebuilt .ex5 instead")
        deploy_src = args.ex5 or (Path(__file__).parent / role["ex5"])
        if deploy_src and Path(deploy_src).exists():
            shutil.copy2(deploy_src, out_ex5)
            logger.info(
                "deployed %s from %s (%d bytes)",
                out_ex5.name, Path(deploy_src).name, out_ex5.stat().st_size,
            )
        elif out_ex5.exists():
            logger.info("reusing existing %s", out_ex5)
        else:
            logger.error(
                "no .ex5 to deploy: pass --ex5 <path>, keep a bundled %s "
                "beside this script, or use --compile",
                role["ex5"],
            )
            return 1

    # 4. Attach the EA to a chart profile.
    if not args.no_attach:
        chart_file = data_dir / "MQL5" / "Profiles" / "Charts" / "DEFAULT" / f"chart{args.chart:02d}.chr"
        if not chart_file.exists():
            existing = sorted(
                p.name for p in chart_file.parent.glob("chart*.chr")
                if CHART_RE.match(p.name)
            )
            logger.error(
                "chart profile %s does not exist (available: %s) — pick a chart with --chart, "
                "or use --no-attach and attach manually",
                chart_file.name, ", ".join(existing) or "none",
            )
            return 1

        inputs = list(role["default_inputs"])
        if args.role == "follower":
            inputs = [("ExpertMagic", str(args.magic)) if k == "ExpertMagic" else (k, v) for k, v in inputs]
        for override in args.inputs:
            key, _, value = override.partition("=")
            if not key:
                parser.error(f"--inputs must be KEY=VALUE, got {override!r}")
            inputs = [(k, value if k == key else v) for k, v in inputs]

        raw = chart_file.read_bytes()
        text, nl = chr_decode(raw)
        block = build_expert_block(ea_name, role["ex5"], inputs, nl)
        new_text = upsert_expert_block(text, block, nl)
        if new_text != text:
            chart_file.write_bytes(chr_encode(new_text, nl))
            logger.info("attached %s to %s", ea_name, chart_file.name)
        else:
            logger.info("%s already attached to %s - nothing to change", ea_name, chart_file.name)

    # 5. Summary + next steps.
    logger.info("install complete:")
    logger.info("  EA:      %s (%s)", role["ex5"], experts_dir)
    logger.info("  chart:   chart%02d.chr (attach at startup; keep it the active tab - OnTimer only fires on the active tab)", args.chart)
    if args.role == "master":
        logger.info("  config:  set master.ea_signals_file: %s", files_dir / "master_signals.txt")
    else:
        logger.info("  config:  set agent terminal_data_path: %s", files_dir)
    logger.info("  restart the terminal to load the new chart profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
