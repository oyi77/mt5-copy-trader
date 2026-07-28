#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Install an MT5 terminal for a follower and detect its Manager API port.

PROVEN APPROACH (July 2026):
  /auto  -> silent install to default path (MetaQuotes installer)
  Copy installation to custom path -> compute new AppData hash -> seed
     origin.txt (UTF-16-LE with BOM) + copy essential AppData files
  mt5.initialize() with credentials -> auto login
  Manager API auto-opens on properly seeded copied installation

Usage:
  # Full setup: install + copy + seed + login
  python install_follower_terminal.py setup ^
      --name Follower1 ^
      --install-dir "C:\Users\MSI\ea-copy\follower_terminals\Follower1" ^
      --login 433903489 ^
      --password "xxx" ^
      --server "Exness-MT5Trial7"

  # Just detect Manager API port on an existing running terminal
  python install_follower_terminal.py detect --install-dir "..."

  # Login to an already-running terminal
  python install_follower_terminal.py login ^
      --install-dir "..." ^
      --login 433903489 ^
      --password "xxx" ^
      --server "Exness-MT5Trial7"
"""

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────
METAQUOTES_URL = (
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
)
DEFAULT_INSTALL_DIR = r"C:\Program Files\MetaTrader 5"
PORT_SCAN_START = 15555
PORT_SCAN_END = 15600

# Files/dirs to exclude when copying AppData (large, terminal-regenerated)
APPDATA_EXCLUDE = {
    "history",
    "ticks",
    "storage",
    "MetaTrader 5.lnk",
    "lastconnection",
}


# ── Helpers ────────────────────────────────────────────────────────────────

def compute_appdata_hash(install_dir: str) -> str:
    """Compute the AppData directory hash for a given install path.

    MT5 uses MD5(path.upper().encode('utf-16-le')) to derive the AppData
    directory name under %LOCALAPPDATA%\\MetaQuotes\\Terminal\\.
    """
    raw = install_dir.upper().encode("utf-16-le")
    return hashlib.md5(raw).hexdigest()


def find_default_install() -> str:
    """Check if MT5 is installed at the default location."""
    if os.path.isfile(os.path.join(DEFAULT_INSTALL_DIR, "terminal64.exe")):
        return DEFAULT_INSTALL_DIR
    return ""


def find_appdata_dir(install_dir: str) -> str:
    """Return the MT5 AppData directory for a given install path."""
    appdata_local = os.environ.get("LOCALAPPDATA", "")
    if not appdata_local:
        return ""
    install_hash = compute_appdata_hash(install_dir)
    return os.path.join(appdata_local, "MetaQuotes", "Terminal", install_hash)


def scan_port(port: int, timeout: float = 0.3) -> bool:
    """Check if a TCP port is open on 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        r = s.connect_ex(("127.0.0.1", port))
        return r == 0
    finally:
        s.close()


def detect_manager_port(max_port: int = PORT_SCAN_END) -> int:
    """Find the Manager API port by scanning 127.0.0.1.

    Scans PORT_SCAN_START..max_port and returns the first open port,
    or 0 if none found.
    """
    for port in range(PORT_SCAN_START, max_port + 1):
        if scan_port(port):
            return port
    return 0


def launch_and_wait_for_port(
    exe_path: str, port_hint: int = 0, timeout: int = 60
) -> int:
    """Launch terminal64.exe and wait for a new Manager API port to appear.

    If port_hint is given, we wait specifically for that port to open.
    Otherwise we scan the range and return the first newly-open port.

    Returns the detected port, or 0 on timeout.
    """
    # Snapshot currently-open ports before launch
    baseline = set()
    for p in range(PORT_SCAN_START, PORT_SCAN_END + 1):
        if scan_port(p):
            baseline.add(p)

    print(f"  Launching: {exe_path}")
    proc = subprocess.Popen([exe_path])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)

        if port_hint:
            if scan_port(port_hint):
                print(f"  ✓ Manager API detected on port {port_hint}")
                return port_hint
        else:
            # Look for a port that wasn't in baseline
            for p in range(PORT_SCAN_START, PORT_SCAN_END + 1):
                if p not in baseline and scan_port(p):
                    print(f"  ✓ Manager API detected on port {p}")
                    return p

    print(f"  ✗ No Manager API port detected after {timeout}s")
    return 0


def download_metaquotes_installer(dest_dir: str) -> str:
    """Download the official MetaQuotes MT5 installer."""
    dest = os.path.join(dest_dir, "mt5setup.exe")
    if os.path.isfile(dest) and os.path.getsize(dest) > 20_000_000:
        print(f"  Using cached: {dest}")
        return dest

    print(f"  Downloading MetaQuotes installer...")
    print(f"  URL: {METAQUOTES_URL}")
    urllib.request.urlretrieve(METAQUOTES_URL, dest)
    size_kb = os.path.getsize(dest) // 1024
    print(f"  Saved: {dest} ({size_kb} KB)")
    return dest


def install_silent(installer_path: str) -> bool:
    """Run the MT5 installer silently via /auto.

    The /auto flag installs to the default path silently (no GUI).
    """
    print(f"  Running installer: {installer_path} /auto")
    result = subprocess.run(
        [installer_path, "/auto"],
        capture_output=True,
        timeout=120,
    )
    print(f"  Exit code: {result.returncode}")
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")[:500]
        if err:
            print(f"  stderr: {err}")
        return False

    default = find_default_install()
    return bool(default)


def copy_installation(src: str, dst: str) -> bool:
    """Copy an installed MT5 directory to a new location.

    The copy is a full clone of the MetaTrader 5 installation. After copying,
    you MUST seed the AppData for the new path (seed_appdata) before launching
    — otherwise the terminal won't start its Manager API on the new path.
    """
    if not os.path.isdir(src):
        print(f"  ✗ Source not found: {src}")
        return False

    if os.path.exists(dst) and not os.path.isdir(dst):
        print(f"  ✗ Destination exists and is not a directory: {dst}")
        return False

    print(f"  Copying:")
    print(f"    from: {src}")
    print(f"    to:   {dst}")

    # Remove destination if exists
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    shutil.copytree(src, dst, ignore_dangling_symlinks=True)
    file_count = sum(len(files) for _, _, files in os.walk(dst))
    print(f"  ✓ Copied ({file_count} files)")

    return True


def seed_appdata(install_dir: str, source_appdata: str) -> str:
    """Pre-seed AppData for a given install path.

    This makes a copied/renamed MT5 installation work with full Manager API
    support. Steps:
      1. Compute MD5 hash of the new path → AppData dir name
      2. Copy essential files from the original AppData
      3. Write origin.txt in UTF-16-LE with BOM pointing to the new path

    Args:
        install_dir: The path to the new MT5 installation (e.g. .../Follower1)
        source_appdata: The existing AppData dir for the source installation

    Returns:
        The AppData directory path, or "" on failure.
    """
    appdata_local = os.environ.get("LOCALAPPDATA", "")
    if not appdata_local:
        print("  ✗ LOCALAPPDATA not set")
        return ""

    install_hash = compute_appdata_hash(install_dir)
    mt5_data_dir = os.path.join(
        appdata_local, "MetaQuotes", "Terminal", install_hash
    )

    print(f"  AppData target: {mt5_data_dir}")

    # Create target directory
    os.makedirs(mt5_data_dir, exist_ok=True)

    # Copy essential files from source AppData (if it exists)
    if source_appdata and os.path.isdir(source_appdata):
        print(f"  Copying from source AppData: {source_appdata}")
        copied = 0
        skipped = 0
        for item in os.listdir(source_appdata):
            if item in APPDATA_EXCLUDE:
                skipped += 1
                continue
            src_path = os.path.join(source_appdata, item)
            dst_path = os.path.join(mt5_data_dir, item)
            try:
                if os.path.isdir(src_path):
                    # Skip large subdirs that are terminal-specific
                    if item not in {"history", "ticks", "storage", "logs", "tester"}:
                        shutil.copytree(
                            src_path, dst_path,
                            ignore_dangling_symlinks=True,
                            dirs_exist_ok=True,
                        )
                        copied += 1
                    else:
                        skipped += 1
                else:
                    shutil.copy2(src_path, dst_path)
                    copied += 1
            except (OSError, shutil.Error) as e:
                print(f"    Warning: could not copy {item}: {e}")
                skipped += 1
        print(f"    Copied: {copied}, Skipped: {skipped}")
    else:
        print(f"  No source AppData to copy from — terminal data will be")
        print(f"  generated on first launch.")

    # Write origin.txt in UTF-16-LE with BOM — CRITICAL
    # MT5 expects the BOM + path to determine if this AppData dir belongs
    # to the current installation path.
    origin_path = os.path.join(mt5_data_dir, "origin.txt")
    with open(origin_path, "wb") as f:
        f.write("\ufeff".encode("utf-16-le"))
        f.write(install_dir.encode("utf-16-le"))

    print(f"  ✓ origin.txt written (UTF-16-LE BOM)")
    return mt5_data_dir


def try_programmatic_login(
    exe_path: str, port: int, login: int, password: str, server: str
) -> bool:
    """Login to the MT5 terminal programmatically via python-mt5.

    This works on fresh installations — Manager API accepts credentials
    directly without manual GUI interaction.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("  ✗ MetaTrader5 package not installed")
        return False

    print(f"  Initializing mt5 (path={exe_path}, port={port})...")
    result = mt5.initialize(
        path=exe_path,
        port=port,
        login=login,
        password=password,
        server=server,
        timeout=30000,
    )

    if result:
        acc = mt5.account_info()
        if acc:
            print(f"  ✓ LOGGED IN as {acc.login}@{acc.server}")
            print(f"    Balance: {acc.balance}  Equity: {acc.equity}")
        else:
            print(f"  ✓ Connected but no account info (login may have failed)")
        mt5.shutdown()
        return True
    else:
        err = mt5.last_error()
        print(f"  ✗ mt5.initialize() failed: {err}")
        mt5.shutdown()
        return False


def ensure_source_appdata(source_install_dir: str) -> str:
    """Ensure source AppData exists, launching the terminal briefly if needed.

    Returns the AppData directory path.
    """
    appdata_dir = find_appdata_dir(source_install_dir)

    # If AppData already exists with origin.txt, we're good
    origin_file = os.path.join(appdata_dir, "origin.txt")
    if os.path.isfile(origin_file):
        return appdata_dir

    # Need to launch the terminal once to generate AppData
    print(f"  Source AppData not found — launching terminal briefly...")
    exe_path = os.path.join(source_install_dir, "terminal64.exe")
    if not os.path.isfile(exe_path):
        print(f"  ✗ terminal64.exe not found at {exe_path}")
        return ""

    # Launch, wait for it to initialize, then kill
    proc = subprocess.Popen([exe_path])
    print(f"  Waiting 15s for terminal to initialize...")
    time.sleep(15)
    proc.terminate()
    time.sleep(2)

    # Check if AppData was created
    if os.path.isdir(appdata_dir) and os.path.isfile(origin_file):
        print(f"  ✓ AppData created at {appdata_dir}")
        return appdata_dir

    print(f"  ✗ AppData still not found at {appdata_dir}")
    return ""


# ── Subcommands ────────────────────────────────────────────────────────────

def cmd_setup(args):
    """Full setup: install → copy → seed AppData → launch → login."""
    install_dir = os.path.abspath(args.install_dir) if args.install_dir else DEFAULT_INSTALL_DIR
    name = args.name
    login = args.login
    password = args.password
    server = args.server
    source_dir = os.path.abspath(args.source_dir or DEFAULT_INSTALL_DIR)

    print(f"{'='*60}")
    print(f"SETUP FOLLOWER: {name}")
    print(f"{'='*60}\n")
    print(f"  Install dir: {install_dir}")
    print(f"  Source dir:  {source_dir}")
    print(f"  Account:     {login}@{server}")
    print()

    # ── Step 0: Check if destination already exists ──
    exe_path = os.path.join(install_dir, "terminal64.exe")
    if os.path.isfile(exe_path):
        print(f"[0/5] ✓ Destination already has terminal64.exe — skipping install/copy")
        # Still need to ensure AppData is seeded (step 3 handles this)
        need_install = False
        need_copy = False
    else:
        need_install = True
        need_copy = install_dir.upper() != source_dir.upper()

    # ── Step 1: Install MT5 to default path (if needed) ──
    if need_install:
        print("[1/5] Downloading MetaQuotes installer...")
        installer = download_metaquotes_installer(os.path.dirname(os.path.abspath(__file__)))
        print()

        print("[2/5] Installing MT5 silently...")
        success = install_silent(installer)
        if not success:
            print("  ✗ Installation failed")
            if not os.path.isfile(os.path.join(DEFAULT_INSTALL_DIR, "terminal64.exe")):
                print(f"  ✗ terminal64.exe not found at {DEFAULT_INSTALL_DIR}")
                print("  Try running the installer manually.")
                return
            print(f"  ✓ terminal64.exe found at {DEFAULT_INSTALL_DIR}")
        print()

    # ── Step 2: Ensure source AppData exists ──
    print("[3/5] Ensuring source AppData exists...")
    source_appdata = ensure_source_appdata(source_dir)
    if not source_appdata:
        print("  ✗ Could not establish source AppData — aborting")
        return
    print()

    # ── Step 3: Copy installation to custom path ──
    step_num = 3
    if need_copy:
        print(f"[{step_num}/5] Copying installation to custom path...")
        copy_installation(source_dir, install_dir)
        if not os.path.isfile(exe_path):
            print(f"  ✗ Copy failed — still no terminal64.exe at {install_dir}")
            return
        print()
        step_num = 4
    else:
        print(f"[{step_num}/5] Using source installation path directly...")
        print()
        step_num = 4

    # ── Step 4: Seed AppData for the new path ──
    print(f"[{step_num}/5] Seeding AppData for new installation path...")
    target_appdata = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "MetaQuotes", "Terminal",
        compute_appdata_hash(install_dir),
    )

    # Only seed if it doesn't exist (or --force)
    if os.path.isdir(target_appdata) and os.path.isfile(
        os.path.join(target_appdata, "origin.txt")
    ):
        print(f"  ✓ AppData already seeded for this path")
    else:
        seed_appdata(install_dir, source_appdata)

    print()
    step_num += 1

    # ── Step 5: Launch terminal and login ──
    print(f"[{step_num}/5] Launching terminal and logging in...")

    # Kill any existing terminal64.exe instances to avoid port conflicts
    subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"],
                   capture_output=True)
    time.sleep(2)

    port = launch_and_wait_for_port(exe_path, timeout=60)

    if not port:
        print(f"\n  ✗ Could not detect Manager API port")
        print(f"  Try launching the terminal manually:")
        print(f"    {exe_path}")
        print(f"  Then run: install_follower_terminal.py detect --install-dir \"{install_dir}\"")
        return

    print()

    if login:
        print(f"  Logging in as {login}@{server}...")
        try_programmatic_login(exe_path, port, login, password, server)
    else:
        print(f"  No credentials provided — terminal running but not logged in.")

    print()
    print(f"{'='*60}")
    print(f"FOLLOWER '{name}' READY!")
    print(f"{'='*60}")
    print(f"\nAdd to config.yaml:\n")
    print(f"""  followers:
    - name: "{name}"
      path: "{exe_path}"
      port: {port}
      login: {login or '<LOGIN>'}
      password: "{password or '<PASSWORD>'}"
      server: "{server or '<SERVER>'}"
      lot_multiplier: 1.0
""")
    print(f"Or POST to API:\n")
    print(f'  curl -X POST http://localhost:5000/api/config/followers \\')
    print(f'    -H "Content-Type: application/json" \\')
    print(f'    -d \'{{')
    print(f'      "name": "{name}",')
    print(f'      "path": "{exe_path}",')
    print(f'      "port": {port},')
    print(f'      "login": {login or "<LOGIN>"},')
    print(f'      "password": "{password or "<PASSWORD>"}",')
    print(f'      "server": "{server or "<SERVER>"}",')
    print(f'      "lot_multiplier": 1.0')
    print(f'    }}\'')


def cmd_detect(args):
    """Detect running Manager API port for an existing installation."""
    install_dir = args.install_dir or DEFAULT_INSTALL_DIR
    exe_path = os.path.join(install_dir, "terminal64.exe")

    if not os.path.isfile(exe_path):
        print(f"  ✗ terminal64.exe not found at {exe_path}")
        return

    port = detect_manager_port()
    if port:
        print(f"  ✓ Manager API detected on port {port}")
        print(f"  Install dir: {install_dir}")
        print(f"  Exe path:    {exe_path}")
        appdata_dir = find_appdata_dir(install_dir)
        print(f"  AppData:     {appdata_dir}")

        # Try to get account info
        try:
            import MetaTrader5 as mt5
            r = mt5.initialize(path=exe_path, port=port, timeout=5000)
            if r:
                acc = mt5.account_info()
                if acc:
                    print(f"  Account:     {acc.login}@{acc.server}")
                    print(f"  Balance:     {acc.balance}")
                mt5.shutdown()
            else:
                print(f"  mt5.initialize failed: {mt5.last_error()}")
        except ImportError:
            pass
    else:
        print(f"  ✗ No Manager API port detected")
        print(f"  Make sure the terminal is running and logged into an account.")


def cmd_login(args):
    """Login to a running terminal programmatically."""
    install_dir = args.install_dir or DEFAULT_INSTALL_DIR
    login = args.login
    password = args.password
    server = args.server

    exe_path = os.path.join(install_dir, "terminal64.exe")
    port = detect_manager_port()

    if not port:
        print(f"  ✗ No Manager API port detected")
        print(f"  Make sure the terminal is running.")
        return

    if not login:
        print(f"  ✗ No login credentials provided")
        return

    try_programmatic_login(exe_path, port, login, password, server)


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Install and configure MT5 follower terminals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Full setup: install + copy + seed + login")
    p_setup.add_argument("--name", required=True, help="Follower name (e.g. Follower1)")
    p_setup.add_argument("--install-dir", default=None,
                         help=f"Installation path (default: {DEFAULT_INSTALL_DIR})")
    p_setup.add_argument("--source-dir", default=None,
                         help=f"Source MT5 installation to copy from (default: {DEFAULT_INSTALL_DIR})")
    p_setup.add_argument("--login", type=int, default=None, help="MT5 account login")
    p_setup.add_argument("--password", default=None, help="MT5 account password")
    p_setup.add_argument("--server", default=None, help="MT5 server name")
    p_setup.add_argument("--force", action="store_true",
                         help="Re-seed AppData even if it already exists")

    # detect
    p_detect = sub.add_parser("detect", help="Detect Manager API port")
    p_detect.add_argument("--install-dir", default=None,
                          help=f"Installation path (default: {DEFAULT_INSTALL_DIR})")

    # login
    p_login = sub.add_parser("login", help="Login to running terminal")
    p_login.add_argument("--install-dir", default=None,
                         help=f"Installation path (default: {DEFAULT_INSTALL_DIR})")
    p_login.add_argument("--login", type=int, required=True, help="MT5 account login")
    p_login.add_argument("--password", required=True, help="MT5 account password")
    p_login.add_argument("--server", required=True, help="MT5 server name")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "detect":
        cmd_detect(args)
    elif args.command == "login":
        cmd_login(args)


if __name__ == "__main__":
    main()
