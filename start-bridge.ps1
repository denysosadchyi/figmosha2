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
    if (-not $cmd) {
        Write-Error "No Python found. Create the venv first: python -m venv venv; .\venv\Scripts\pip install aiohttp"
    }
    $python = $cmd.Source
    Write-Host "venv not found, using $python" -ForegroundColor Yellow
}

function Get-BridgePid {
    $line = netstat -ano | Select-String ":$Port\s" | Select-String "LISTENING" | Select-Object -First 1
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
        Write-Error "could not stop pid ${existing}: $($_.Exception.Message). It may be running as another user — try an elevated shell."
    }
}

if ($Stop -or $Restart) { Stop-Bridge }
if ($Stop) { return }

$existing = Get-BridgePid
if ($existing) {
    Write-Host "bridge already listening on $Port (pid $existing) — use -Restart to replace it" -ForegroundColor Yellow
    return
}

Start-Process -FilePath $python `
    -ArgumentList @("-u", (Join-Path $root "bridge.py"), "--port", $Port) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $root "bridge.err.log") `
    -WindowStyle Hidden | Out-Null

Start-Sleep -Seconds 2
$now = Get-BridgePid
if ($now) {
    Write-Host "bridge listening on http://127.0.0.1:$Port (pid $now)" -ForegroundColor Green
    Write-Host "log: $log"
    Write-Host "now run the plugin: Figma → Plugins → Development → Figmosha Bridge"
} else {
    Write-Host "bridge did not come up — check $log and bridge.err.log" -ForegroundColor Red
    if (Test-Path $log) { Get-Content $log -Tail 20 }
}
