# Start the Figmosha bridge on native Windows, detached.
#
# start-bridge.sh needs bash and tmux, which a plain Windows box has neither of;
# without this the only option is keeping a terminal window open forever.
#
#   .\start-bridge.ps1            # start (no-op if already running)
#   .\start-bridge.ps1 -Restart   # stop whatever holds the port, then start
#   .\start-bridge.ps1 -Stop      # just stop
#
# Logs go to bridge.out.log next to this script.
#
# Keep this file pure ASCII: Windows PowerShell 5.1 reads a BOM-less script as
# ANSI, so a single em dash or arrow breaks parsing of the whole file.

param(
    [int]$Port = 8787,
    [switch]$Restart,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root "bridge.out.log"

# Prefer the venv interpreter, fall back to whatever python is on PATH.
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    # WindowsApps\python.exe is the Microsoft Store stub: it opens the Store
    # instead of running anything, so treat it as "no Python".
    if ($cmd -and $cmd.Source -like "*\WindowsApps\*") { $cmd = $null }
    if (-not $cmd) {
        Write-Error "No Python found. Install it from python.org (tick 'Add to PATH'), then: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt"
    }
    $python = $cmd.Source
    Write-Host "venv not found, using $python" -ForegroundColor Yellow
}

function Get-BridgePid {
    # Get-NetTCPConnection reports the state as an enum, so it works on any
    # Windows display language; netstat prints "LISTENING" translated.
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    $line = netstat -ano | Select-String ":$Port\s.*\s0\.0\.0\.0:0\s|:$Port\s.*\s\[::\]:0\s" | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line.ToString() -split '\s+' | Where-Object { $_ } | Select-Object -Last 1)
}

function Stop-Bridge {
    $existing = Get-BridgePid
    if (-not $existing) { Write-Host "port $Port is free"; return }
    try {
        Stop-Process -Id $existing -Force -ErrorAction Stop
        Write-Host "stopped pid $existing" -ForegroundColor Green
        Start-Sleep -Milliseconds 600
    } catch {
        Write-Error "could not stop pid ${existing}: $($_.Exception.Message). It may be running as another user - try an elevated shell."
    }
}

if ($Stop -or $Restart) { Stop-Bridge }
if ($Stop) { return }

$existing = Get-BridgePid
if ($existing) {
    Write-Host "bridge already listening on $Port (pid $existing) - use -Restart to replace it" -ForegroundColor Yellow
    return
}

# One pre-quoted string, not an array: PS 5.1 does not quote array elements,
# so a path with a space ("F:\00 Projects\...") splits into two arguments.
Start-Process -FilePath $python `
    -ArgumentList "-u `"$(Join-Path $root 'bridge.py')`" --port $Port" `
    -WorkingDirectory $root `
    -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $root "bridge.err.log") `
    -WindowStyle Hidden | Out-Null

Start-Sleep -Seconds 2
$now = Get-BridgePid
if ($now) {
    Write-Host "bridge listening on http://127.0.0.1:$Port (pid $now)" -ForegroundColor Green
    Write-Host "log: $log"
    Write-Host "now run the plugin: Figma > Plugins > Development > Figmosha Bridge"
} else {
    # Python puts the actual reason (a missing aiohttp, a taken port) on stderr.
    $errLog = Join-Path $root "bridge.err.log"
    Write-Host "bridge did not come up - last lines of bridge.err.log:" -ForegroundColor Red
    if (Test-Path $errLog) { Get-Content $errLog -Tail 20 }
    if ((Get-Content $errLog -Raw -ErrorAction SilentlyContinue) -match "No module named 'aiohttp'") {
        Write-Host "fix: pip install -r requirements.txt (with the venv's pip if you use one)" -ForegroundColor Yellow
    }
    exit 1
}
