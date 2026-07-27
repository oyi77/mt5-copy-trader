#!/usr/bin/env python3
"""Package distribution zip for easy deployment."""

import os
import shutil


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # Create a staging directory
    stage = "dist/staging"
    if os.path.exists(stage):
        shutil.rmtree(stage, ignore_errors=True)

    os.makedirs(os.path.join(stage, "master"))
    os.makedirs(os.path.join(stage, "follower"))

    # Copy run.exe + its folder contents
    run_src = "dist/run"
    run_dst = os.path.join(stage, "master")
    for item in os.listdir(run_src):
        s = os.path.join(run_src, item)
        d = os.path.join(run_dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
        else:
            shutil.copy2(s, d)

    # Copy agent.exe + its folder contents
    agent_src = "dist/agent"
    agent_dst = os.path.join(stage, "follower")
    for item in os.listdir(agent_src):
        s = os.path.join(agent_src, item)
        d = os.path.join(agent_dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
        else:
            shutil.copy2(s, d)

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
  1. Double-click master/run.exe
  2. On first run, it creates config.yaml - edit it:
     - Set master terminal path & port
     - (Optional) Add same-PC followers
  3. Restart run.exe
  4. Open http://localhost:5000 in browser for dashboard

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
  - Both exes auto-create default config files on first run
""")

    # Zip it
    zip_path = shutil.make_archive("dist/copy-trade-engine", "zip", stage)
    size = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"Created {zip_path} ({size:.1f} MB)")

    # Cleanup
    shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
