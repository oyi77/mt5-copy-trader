#!/usr/bin/env python3
"""Package distribution zip for easy deployment."""

import os
import shutil


def copy_bundle(src_dir: str, dst_dir: str, exe_name: str) -> None:
    """Copy a PyInstaller bundle into the staging dir.

    Handles both layouts: onedir (dist/<name>/<name>.exe + _internal/) and
    onefile (dist/<name>.exe).
    """
    exe_path = os.path.join(os.path.dirname(src_dir), f"{exe_name}.exe")
    if os.path.isdir(src_dir):
        for item in os.listdir(src_dir):
            s = os.path.join(src_dir, item)
            d = os.path.join(dst_dir, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
            else:
                shutil.copy2(s, d)
    elif os.path.isfile(exe_path):
        shutil.copy2(exe_path, os.path.join(dst_dir, f"{exe_name}.exe"))
    else:
        raise FileNotFoundError(
            f"Build output not found: {src_dir} (onedir folder) or {exe_path} (onefile exe)"
        )


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # Create a staging directory
    stage = "dist/staging"
    if os.path.exists(stage):
        shutil.rmtree(stage, ignore_errors=True)

    os.makedirs(os.path.join(stage, "master"))
    os.makedirs(os.path.join(stage, "follower"))

    # Copy run.exe (onedir folder or onefile exe)
    copy_bundle("dist/run", os.path.join(stage, "master"), "run")
    # Copy agent.exe (onedir folder or onefile exe)
    copy_bundle("dist/agent", os.path.join(stage, "follower"), "agent")

    # Ship sample configs — they are NOT baked into the exes (runtime artifacts)
    if os.path.exists("config.yaml"):
        shutil.copy2("config.yaml", os.path.join(stage, "master", "config.yaml"))
    if os.path.exists("agent_config_deploy.yaml"):
        shutil.copy2("agent_config_deploy.yaml", os.path.join(stage, "follower", "agent_config.yaml"))

    # Write README
    with open(os.path.join(stage, "README.txt"), "w") as f:
        f.write("""\
Copy Trade Engine - MT5 Trade Copier
=====================================
Master <-> Follower trade replication via WebSocket hub.

FILES:
  master/run.exe       -> Run on the master trading PC
  follower/agent.exe   -> Run on each follower PC

QUICK START (MASTER):
  1. Edit config.yaml (shipped next to run.exe):
     - Set master terminal path & port
     - (Optional) Add same-PC followers
  2. Double-click master/run.exe
  3. Open http://localhost:5000 in browser for dashboard

FOLLOWER SETUP:
  1. Install Tailscale on all machines (or use LAN IPs)
  2. Copy the follower/ folder to each follower PC
  3. Double-click follower/agent.exe
  4. Edit agent_config.yaml:
     - Set hub_url to the master machine's IP:5000
     - Set MT5 login credentials
  5. Restart agent.exe

NOTES:
  - MetaTrader 5 must be installed on both machines
  - MT5 Expert Advisors tab must have a unique port set
  - MT5 must be logged in with the trading account
  - Configs are not baked into the exes: edit the shipped config.yaml /
    agent_config.yaml before first run (they contain your login credentials)
""")

    # Zip it
    zip_path = shutil.make_archive("dist/copy-trade-engine", "zip", stage)
    size = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"Created {zip_path} ({size:.1f} MB)")

    # Cleanup
    shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
