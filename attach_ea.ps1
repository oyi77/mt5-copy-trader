# attach_ea.ps1 - attach an EA to a chart in an MT5 terminal (UI automation)
#
# Re-attaches TradeSender (master side) or TradeReceiver (follower side) after
# a hard kill of the terminal wiped the chart profile. Used manually and as the
# fallback by run.py's EA watchdog (master.ea_watchdog_attach_script).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File attach_ea.ps1 -TerminalPath "C:\...\terminal64.exe"
#   powershell -ExecutionPolicy Bypass -File attach_ea.ps1 -TerminalPath "C:\...\terminal64.exe" -Symbol XAUUSD -EAName TradeReceiver -DataDir "C:\...\Terminal\<hash>"
#
# Notes: SendKeys requires the terminal window to be foreground; the script
# uses a forced foreground trick that works even when another window has focus.
# The chart is opened via the symbol dialog (Ctrl+N). The EA is attached by
# DOUBLE-CLICKING its row in the Navigator tree: MT5's Insert > Expert Advisor
# dialog does NOT select the EA when the name is typed (incremental search
# misfires), so the Insert-menu path is not used. Double-clicking pops the
# "attach instead of ..." confirmation and then the properties dialog; the
# script confirms both.
#
# Verification: with -DataDir set, the script reads the terminal journal
# (<DataDir>\logs\YYYYMMDD.log, UTF-16) and waits for a fresh
# "expert <EAName> ... loaded successfully" line after the attach, retrying up
# to 3 times. A live signal-file heartbeat is NOT used as the sole check,
# because an already-attached EA keeps heartbeating and would mask a failed
# attach. Without -DataDir the script assumes success (no way to verify).
#
# Coordinates are relative to the window's top-left (navigator rows sit at
# fixed offsets in the docked panel; dialogs are centered). Defaults are tuned
# for the default layout with "Expert Advisors" expanded at 1080p. If your
# layout differs, pass -NavRowY to point at the exact row of the target EA.

param(
    [Parameter(Mandatory = $true)][string]$TerminalPath,
    [string]$Symbol = "BTCUSD",
    [string]$EAName = "TradeSender",
    [string]$DataDir = "",
    [int]$WaitSeconds = 3,
    [int]$NavRowY = -1   # window-relative Y of the EA row in the Navigator tree
)

$ErrorActionPreference = "Continue"
Add-Type -AssemblyName System.Windows.Forms

# Get-Process.Path returns canonical backslash paths; configs often carry
# forward-slash paths (e.g. from YAML), so normalize before comparing.
$TerminalPath = [System.IO.Path]::GetFullPath($TerminalPath)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Att {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    public static void ForceForeground(IntPtr hWnd) {
        uint fgThread, targetThread;
        GetWindowThreadProcessId(GetForegroundWindow(), out fgThread);
        GetWindowThreadProcessId(hWnd, out targetThread);
        uint me = GetCurrentThreadId();
        if (fgThread != 0 && fgThread != me) AttachThreadInput(me, fgThread, true);
        if (targetThread != 0 && targetThread != me) AttachThreadInput(me, targetThread, true);
        keybd_event(0x12, 0, 0, UIntPtr.Zero);
        SetForegroundWindow(hWnd);
        keybd_event(0x12, 0, 2, UIntPtr.Zero);
        ShowWindow(hWnd, 5);
        BringWindowToTop(hWnd);
        SetFocus(hWnd);
        if (fgThread != 0 && fgThread != me) AttachThreadInput(me, fgThread, false);
        if (targetThread != 0 && targetThread != me) AttachThreadInput(me, targetThread, false);
    }
    public static void Click(int x, int y, bool dbl) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(200);
        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
        if (dbl) {
            System.Threading.Thread.Sleep(120);
            mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
            mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
        }
    }
}
"@

function Send-Keys([string]$keys, [int]$delayMs = 400) {
    [System.Windows.Forms.SendKeys]::SendWait($keys)
    Start-Sleep -Milliseconds $delayMs
}

Write-Host "=== Attach $EAName to $Symbol ==="

$proc = Get-Process terminal64 -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $TerminalPath -and $_.MainWindowHandle -ne [IntPtr]::Zero } |
    Select-Object -First 1
if (-not $proc) {
    Write-Host "Terminal not running ($TerminalPath). Starting it..."
    Start-Process -FilePath $TerminalPath
    Start-Sleep -Seconds 15
    $proc = Get-Process terminal64 -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $TerminalPath -and $_.MainWindowHandle -ne [IntPtr]::Zero } |
        Select-Object -First 1
}
if (-not $proc) {
    Write-Host "ERROR: could not find or start terminal at $TerminalPath"
    exit 1
}

$hwnd = $proc.MainWindowHandle
Write-Host "PID=$($proc.Id) hwnd=0x$($hwnd.ToString('X'))"

$got = $false
for ($i = 1; $i -le 8; $i++) {
    [Win32Att]::ForceForeground($hwnd) | Out-Null
    Start-Sleep -Milliseconds 300
    if ([Win32Att]::GetForegroundWindow() -eq $hwnd) { $got = $true; break }
}
if (-not $got) {
    Write-Host "ERROR: could not bring terminal window to foreground"
    exit 2
}
Write-Host "foreground OK"

# Window top-left (all offsets below are window-relative)
$rect = New-Object Win32Att+RECT
[Win32Att]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
$wl = $rect.Left
$wt = $rect.Top
Write-Host "window rect: L=$wl T=$wt R=$($rect.Right) B=$($rect.Bottom)"

# 1) Open a chart for $Symbol: Ctrl+N opens the symbol dialog.
#    Dismiss any stray dialog first (e.g. a Login dialog left open by a
#    previous misclick on an account row in the Navigator tree).
Send-Keys "{ESC}" 300
Send-Keys "{ESC}" 300
Send-Keys "^{n}" 1200
Send-Keys $Symbol 800
Send-Keys "{ENTER}" 2500
Write-Host "chart open attempted ($Symbol)"

# 2) Attach the EA by double-clicking its row in the Navigator tree, then
#    confirm the "attach instead of ..." dialog (Yes) and the properties
#    dialog (OK). A single click on empty chart space is harmless, so the
#    confirm clicks are safe even when no dialog popped up.
#
#    The Navigator tree layout is NOT fixed: the panel can be short and the
#    tree scrolled down (e.g. an expanded "Accounts" section fills the
#    viewport), so a hard-coded row offset may land on an account row and
#    open a Login dialog. To stay robust we (a) scroll the tree to the top
#    with HOME after focusing it, then (b) sweep candidate row positions,
#    verifying each attempt against the terminal journal, which is the
#    ground truth for "the EA really loaded".
$rowX = $wl + 112   # left column of the Navigator tree
if ($NavRowY -gt 0) {
    # Explicit override: single candidate row (window-relative offset).
    $candidates = @($NavRowY)
} else {
    # Focus the tree (single click) and scroll it to the top.
    [Win32Att]::Click($rowX, $wt + 360, $false) | Out-Null
    Start-Sleep -Milliseconds 400
    Send-Keys "{HOME}" 800
    # Tuned defaults first (fast path when the layout matches), then a sweep
    # over the visible tree viewport (rows are ~20px apart).
    if ($EAName -eq "TradeSender") { $defaults = @(509) }
    elseif ($EAName -eq "TradeReceiver") { $defaults = @(489) }
    else { $defaults = @() }
    $sweep = @(340, 360, 380, 400, 420, 440, 460, 480, 500, 520, 540)
    $candidates = $defaults + $sweep
}

function Attach-EA([int]$rowY) {
    # Re-assert foreground (a prior candidate may have opened a dialog).
    [Win32Att]::ForceForeground($hwnd) | Out-Null
    Start-Sleep -Milliseconds 200
    # ESC clears any Login dialog a previous candidate may have opened.
    Send-Keys "{ESC}" 250
    Send-Keys "{ESC}" 250
    [Win32Att]::Click($rowX, $wt + $rowY, $true) | Out-Null
    Start-Sleep -Milliseconds 2000
    # Confirm "Do you really want to attach X instead of Y?" -> Yes
    [Win32Att]::Click($wl + 1015, $wt + 597, $false) | Out-Null
    Start-Sleep -Milliseconds 1500
    # Confirm the EA properties dialog -> OK
    [Win32Att]::Click($wl + 1076, $wt + 710, $false) | Out-Null
    Start-Sleep -Milliseconds 1500
    Write-Host "EA attach attempted ($EAName at row y=$rowY)"
}

# 3) Verify: read the terminal journal for a fresh "loaded successfully"
#    line for $EAName. The journal is UTF-16LE at <DataDir>\logs\YYYYMMDD.log.
$journal = ""
if ($DataDir -and (Test-Path $DataDir)) {
    $today = (Get-Date).ToString("yyyyMMdd")
    $candidate = Join-Path $DataDir "logs\$today.log"
    $yesterday = (Get-Date).AddDays(-1).ToString("yyyyMMdd")
    $ycand = Join-Path $DataDir "logs\$yesterday.log"
    if (Test-Path $candidate) { $journal = $candidate }
    elseif (Test-Path $ycand) { $journal = $ycand }
    Write-Host "journal: $journal (exists=$(Test-Path $journal))"
}
$sig = ""
if ($DataDir -and (Test-Path $DataDir)) {
    $sig = Join-Path $DataDir "MQL5\Files\master_signals.txt"
}

function Get-JournalText {
    # Read UTF-16LE journal as text, tolerating the terminal's open write
    # handle (FileShare.ReadWrite). Skip a 2-byte BOM if present.
    $fs = [System.IO.File]::Open($journal, [System.IO.FileMode]::Open,
          [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $bytes = New-Object byte[] $fs.Length
        [void]$fs.Read($bytes, 0, $bytes.Length)
    } finally {
        $fs.Dispose()
    }
    $enc = New-Object System.Text.UnicodeEncoding($false, $true)
    return $enc.GetString($bytes)
}

$attached = $false
foreach ($rowY in $candidates) {
    if ($attached) { break }
    $before = ""
    if ($journal -and (Test-Path $journal)) { $before = Get-JournalText }

    function Get-LoadTime([string]$text) {
        # Newest journal line "expert <EAName> (...) loaded successfully"
        # -> its HH:MM:SS timestamp, or "" when absent.
        $lines = $text -split "`r?`n"
        for ($i = $lines.Length - 1; $i -ge 0; $i--) {
            $ln = $lines[$i]
            if ($ln -match [regex]::Escape($EAName) -and $ln -match "loaded successfully") {
                if ($ln -match "(\d{2}):(\d{2}):(\d{2})") {
                    return $matches[0]
                }
                return "found"
            }
        }
        return ""
    }

    Attach-EA $rowY
    if ($journal -and (Test-Path $journal)) {
        # A fresh load line appears within a couple of seconds of a real
        # attach, so 8s per candidate bounds the sweep (~2 min worst case,
        # within the watchdog's 120s attach-script timeout).
        $deadline = (Get-Date).AddSeconds(8)
        $ok = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            $after = Get-JournalText
            $t1 = Get-LoadTime $before
            $t2 = Get-LoadTime $after
            if ($t1 -ne "" -and $t2 -ne $t1) {
                # A NEWER "loaded successfully" line appeared.
                $ok = $true
                break
            }
            if ($t1 -eq "" -and $t2 -ne "") {
                # First load line appeared at all.
                $ok = $true
                break
            }
        }
        if ($ok) {
            $attached = $true
            Write-Host "journal shows a fresh $EAName load (row y=$rowY) - EA is live"
            if ($sig -and (Test-Path $sig)) { Get-Content $sig -Tail 2 }
            break
        }
        Write-Host "no fresh load line at row y=$rowY, trying next..."
    } else {
        # No journal to verify against - assume the attach worked.
        $attached = $true
        break
    }
}
if (-not $attached) {
    Write-Host "ERROR: $EAName did not appear in the journal after sweeping the navigator tree"
    exit 3
}
Write-Host "Done."
