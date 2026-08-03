#!/usr/bin/env python3
"""Build standalone exes for Copy Trade Engine using PyInstaller.

Produces two executables under dist/:
  run.exe    — Master: bridge + dashboard server (needs MT5 on this machine)
  agent.exe  — Follower: connects to hub, executes on local MT5

Usage:
    python build.py                  # build both (onedir, fast startup)
    python build.py --onefile        # single-file exes (one .exe each)
    python build.py --clean          # rebuild from scratch
"""

import os
import shutil
import subprocess
import sys

SEP = ";" if sys.platform == "win32" else ":"


def build_exe(name: str, entry: str, console: bool = True) -> None:
    """Build one exe with PyInstaller."""
    print(f"\n{'='*60}")
    print(f"  Building {name}.exe from {entry}")
    print(f"{'='*60}")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", name,
        "--noconfirm",
        "--console" if console else "--noconsole",
    ]

    # onefile or onedir
    if "--onefile" in sys.argv:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    # Bundle static files (only needed by run.exe, harmless for agent)
    cmd.extend(["--add-data", f"static{SEP}static"])

    # Runtime artifacts (config.yaml, logs/, event_store.db*, scratch/) are NOT
    # bundled: configs contain live credentials and must ship next to the exe.

    # src/ package — force explicit inclusion of every module (imports are
    # static today, this guards against future dynamic-import regressions)
    for mod in [
        "src.config", "src.models", "src.state", "src.bridge",
        "src.master", "src.follower", "src.server", "src.agent_client",
    ]:
        cmd.extend(["--hidden-import", mod])

    # MetaTrader5 — native .pyd, force explicit inclusion
    cmd.extend(["--hidden-import", "MetaTrader5"])

    # psutil — used for process discovery in auto-trading enabler
    cmd.extend(["--hidden-import", "psutil"])

    # aiohttp deps sometimes missed
    cmd.extend(["--hidden-import", "multidict._multidict_py"])

    cmd.append(entry)

    # Clean previous build artefacts for this name
    for target in [f"build/{name}", f"dist/{name}"]:
        if os.path.exists(target):
            try:
                shutil.rmtree(target, ignore_errors=False)
            except PermissionError:
                print(f"  WARNING: Could not remove {target} (file in use)")
                # Try once more after a brief wait
                import time
                time.sleep(1)
                try:
                    shutil.rmtree(target, ignore_errors=True)
                except Exception:
                    pass

    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd)
    print(f"  [OK] {name}.exe built")


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    if "--clean" in sys.argv:
        for d in ["build", "dist"]:
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)
                print(f"Cleaned {d}")

    os.makedirs("dist", exist_ok=True)

    # Build both
    build_exe("run", "run.py", console=True)
    build_exe("agent", "agent.py", console=False)

    # Summary
    print(f"\n{'='*60}")
    print(f"  BUILD COMPLETE")
    print(f"{'='*60}")
    onefile = "--onefile" in sys.argv
    for name in ["run", "agent"]:
        if onefile:
            path = f"dist/{name}.exe"
        else:
            path = f"dist/{name}/{name}.exe"
        if os.path.exists(path):
            size = os.path.getsize(path) / (1024 * 1024)
            print(f"  {path}  ({size:.1f} MB)")
        else:
            print(f"  ? {path} not found")
    print()
    print("  To deploy:")
    print("    dist/run.exe   -> master machine with MT5")
    print("    dist/agent.exe -> each follower machine")
    print()


if __name__ == "__main__":
    main()
