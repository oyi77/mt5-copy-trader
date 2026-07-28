#!/usr/bin/env python3
"""Install an MT5 terminal for a follower and detect its Manager API port.

Usage:
    # New follower: install, detect, login
    python install_follower_terminal.py setup Follower1 ^
        --login 12345678 --password "sekret" --server "Exness-MT5Trial7" ^
        --install-dir "C:\Program Files\MetaTrader 5 Follower1"

    # Existing terminal: just detect port
    python install_follower_terminal.py detect --install-dir "C:\Program Files\MetaTrader 5 Follower1"

    # Existing terminal with known path + login
    python install_follower_terminal.py login ^
        --path "C:\Program Files\MetaTrader 5 Follower1\terminal64.exe" ^
        --login 12345678 --password "sekret" --server "Exness-MT5Trial7"

What this does:
  1. Downloads the MetaQuotes installer (not broker-specific — only MetaQuotes
     installer supports /auto for silent install)
  2. Runs mt5setup.exe /auto → silent install to default Program Files path
  3. Copies installation to --install-dir if specified (if Manager API doesn't
     start from copied location, tells user to run GUI installer once)
  4. Launches terminal, waits for Manager API port to open
  5. Logs in programmatically via mt5.initialize() — no manual GUI needed
  6. Outputs config.yaml snippet

Key findings (proven by testing):
  ✅ /auto  → silent install, no GUI (works on MetaQuotes installer)
  ❌ /path: → documented but NOT supported by current installer builds
  ❌ Copying installation → Manager API doesn't start (IPC timeout)
  ✅ mt5.initialize() with credentials → auto login on fresh install
  ✅ Manager API auto-opens on properly installed terminal
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


# ── Helpers ────────────────────────────────────────────────────────────────

def compute_appdata_hash(install_dir: str) -> str:
    """Compute the AppData directory hash for a given install path.

    MT5 uses MD5(path.upper().encode('utf-16-le')) to derive the AppData
    directory name under %LOCALAPPDATA%\MetaQuotes\Terminal\.
    """
    raw = install_dir.upper().encode("utf-16-le")
    return hashlib.md5(raw).hexdigest()


def find_default_install() -> str:
    """Check if MT5 is installed at the default location."""
    if os.path.isfile(os.path.join(DEFAULT_INSTALL_DIR, "terminal64.exe")):
        return DEFAULT_INSTALL_DIR
    return ""


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
    """Find the Manager API port by scanning."""
    for port in range(PORT_SCAN_START, max_port + 1):
        if scan_port(port):
            return port
    return 0


def wait_for_terminal(exe_path: str, timeout: int = 30) -> int:
    """Launch terminal64.exe and wait for its Manager API port.

    Returns the detected port, or 0 on timeout.
    """
    print(f"  Launching: {exe_path}")
    proc = subprocess.Popen([exe_path])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = detect_manager_port()
        if port:
            print(f"  ✓ Manager API detected on port {port}")
            return port
        time.sleep(1)

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

    The /auto flag installs to the default path silently (no GUI). The /path:
    flag is documented but NOT supported by current installer builds, so we
    always install to default and optionally copy afterward.
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

    NOTE: This is best-effort. Tested copies of MT5 installations fail to
    start the Manager API (IPC timeout). Only running the installer properly
    enables it. This function warns the user about this limitation.
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
    print(f"  ⚠  Copied MT5 installations may not start the Manager API.")
    print(f"  ⚠  If it fails, run the GUI installer once to this path.")

    # Remove destination if exists
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    shutil.copytree(src, dst, ignore_dangling_symlinks=True)
    print(f"  ✓ Copied ({sum(len(files) for _, _, files in os.walk(dst))} files)")

    return True


def seed_appdata(install_dir: str) -> str:
    """Pre-seed AppData for a given install path so the terminal doesn't
    generate a different hash on first launch.

    Returns the AppData directory path.
    """
    appdata_local = os.environ.get("LOCALAPPDATA", "")
    if not appdata_local:
        return ""

    install_hash = compute_appdata_hash(install_dir)
    mt5_data_dir = os.path.join(
        appdata_local, "MetaQuotes", "Terminal", install_hash
    )

    os.makedirs(mt5_data_dir, exist_ok=True)

    # Write origin.txt pointing to the install path
    origin_path = os.path.join(mt5_data_dir, "origin.txt")
    with open(origin_path, "w") as f:
        f.write(install_dir)

    print(f"  AppData: {mt5_data_dir}")
    return mt5_data_dir


def try_programmatic_login(
    exe_path: str, port: int, login: int, password: str, server: str
) -> bool:
    """Login to the MT5 terminal programmatically via python-mt5.

    This works on fresh installations — the Manager API accepts login
    credentials directly without manual GUI interaction.
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


# ── Subcommands ────────────────────────────────────────────────────────────

def cmd_setup(args):
    """Install, detect, and login a new follower terminal."""
    install_dir = args.install_dir or DEFAULT_INSTALL_DIR
    name = args.name
    login = args.login
    password = args.password
    server = args.server

    print(f"{'='*60}")
    print(f"SETUP FOLLOWER: {name}")
    print(f"{'='*60}\n")

    # 1. Download installer
    print("[1/4] Downloading MetaQuotes installer...")
    installer = download_metaquotes_installer(os.path.dirname(os.path.abspath(__file__)))
    print()

    # 2. Install silently to default path
    print("[2/4] Installing MT5 silently...")
    success = install_silent(installer)
    if not success:
        print("  ✗ Installation failed")
        default_path = DEFAULT_INSTALL_DIR
        if not os.path.isfile(os.path.join(default_path, "terminal64.exe")):
            print(f"  ✗ terminal64.exe not found at {default_path}")
            print("  Try running the installer manually.")
            return
        print(f"  ✓ terminal64.exe found at {default_path}")
    print()

    # 3. If custom path requested, copy installation there
    exe_path = os.path.join(install_dir, "terminal64.exe")
    if install_dir.upper() != DEFAULT_INSTALL_DIR.upper():
        print("[3/4] Setting up custom installation path...")
        if not os.path.isfile(exe_path):
            copy_installation(DEFAULT_INSTALL_DIR, install_dir)
            seed_appdata(install_dir)

        if os.path.isfile(exe_path):
            print(f"  ✓ terminal64.exe at {exe_path}")
        else:
            print(f"  ✗ Still no terminal64.exe at {install_dir}")
            print(f"  Falling back to default path")
            exe_path = os.path.join(DEFAULT_INSTALL_DIR, "terminal64.exe")
            install_dir = DEFAULT_INSTALL_DIR
    else:
        print("[3/4] Using default installation path...")
        exe_path = os.path.join(DEFAULT_INSTALL_DIR, "terminal64.exe")
    print()

    # 4. Wait for Manager API port
    print("[4/4] Detecting Manager API port and logging in...")
    print(f"  Checking if terminal is already running...")
    port = detect_manager_port()
    if not port:
        # Kill existing terminals to avoid port conflict
        subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"],
                       capture_output=True)
        time.sleep(2)

        port = wait_for_terminal(exe_path, timeout=30)

    if not port:
        print(f"\n  ✗ Could not detect Manager API port")
        print(f"  Try launching the terminal manually and logging in:")
        print(f"    {exe_path}")
        print(f"  Then run: install_follower_terminal.py detect --install-dir \"{install_dir}\"")
        return
    print()

    # 5. Programmatic login
    if login:
        print(f"  Logging in as {login}@{server}...")
        try_programmatic_login(exe_path, port, login, password, server)
    else:
        print(f"  No credentials provided — terminal is running but not logged in.")

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
    print(f"Or POST to API: POST /api/config/followers with JSON body")


def cmd_detect(args):
    """Detect running Manager API port for an existing installation."""
    install_dir = args.install_dir or DEFAULT_INSTALL_DIR
    exe_path = os.path.join(install_dir, "terminal64.exe")

    print(f"Detecting Manager API for: {install_dir}")

    if not os.path.isfile(exe_path):
        print(f"  ✗ No terminal64.exe found")
        return

    port = detect_manager_port()
    if port:
        print(f"  ✓ Manager API port: {port}")
    else:
        print(f"  Terminal not running or not logged in. Attempting to launch...")
        port = wait_for_terminal(exe_path, timeout=20)

    if port:
        print(f"\nConfig snippet:")
        print(f"  path: \"{exe_path}\"")
        print(f"  port: {port}")
    else:
        print(f"\nCould not detect Manager API port.")
        print(f"Make sure the terminal is running and logged into an account.")


def cmd_login(args):
    """Login to a running terminal programmatically."""
    exe_path = os.path.abspath(args.path)
    login = args.login
    password = args.password
    server = args.server
    port = args.port or detect_manager_port()

    if not port:
        print("No Manager API port detected. Is the terminal running?")
        return

    print(f"Logging in to {exe_path} (port {port})...")
    try_programmatic_login(exe_path, port, login, password, server)


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Install and configure MT5 follower terminals"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Full setup: install + detect + login")
    p_setup.add_argument("name", help="Follower name")
    p_setup.add_argument("--install-dir", default="",
                         help="Installation directory (default: C:\\Program Files\\MetaTrader 5)")
    p_setup.add_argument("--login", type=int, required=True, help="Account login")
    p_setup.add_argument("--password", required=True, help="Account password")
    p_setup.add_argument("--server", required=True, help="Broker server name")

    # detect
    p_detect = sub.add_parser("detect", help="Detect Manager API port")
    p_detect.add_argument("--install-dir", default="",
                          help="Installation directory (default: C:\\Program Files\\MetaTrader 5)")

    # login
    p_login = sub.add_parser("login", help="Login to running terminal")
    p_login.add_argument("--path", required=True, help="Path to terminal64.exe")
    p_login.add_argument("--port", type=int, default=0, help="Manager API port (auto-detect if omitted)")
    p_login.add_argument("--login", type=int, required=True, help="Account login")
    p_login.add_argument("--password", required=True, help="Account password")
    p_login.add_argument("--server", required=True, help="Broker server name")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "detect":
        cmd_detect(args)
    elif args.command == "login":
        cmd_login(args)


if __name__ == "__main__":
    main()
