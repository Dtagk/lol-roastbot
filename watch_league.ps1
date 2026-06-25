$lockfile = "C:\Riot Games\League of Legends\lockfile"
$botDir   = $PSScriptRoot
$python   = "$botDir\.venv\Scripts\python.exe"
$proxy    = "$botDir\lcu_proxy.py"

# Load .env so LCU_PROXY_SECRET and LCU_PROXY_PORT are available to the proxy.
$envFile = "$botDir\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^([^#=][^=]*)=(.*)$") {
            [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim())
        }
    }
}

# Start the LCU proxy (runs natively so it can reach 127.0.0.1).
$proxyProc = Get-Process -Name python -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -eq "" } |
    Select-Object -First 0  # placeholder; we always (re)start it

Write-Host "Starting LCU proxy..."
$proxyProc = Start-Process -FilePath $python -ArgumentList $proxy `
    -WindowStyle Hidden -PassThru
Write-Host "LCU proxy PID $($proxyProc.Id)"

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path   = Split-Path $lockfile
$watcher.Filter = Split-Path $lockfile -Leaf
$watcher.EnableRaisingEvents = $true

Write-Host "Watching for League client..."

while ($true) {
    # Restart proxy if it crashed.
    if ($proxyProc.HasExited) {
        Write-Host "LCU proxy exited — restarting..."
        $proxyProc = Start-Process -FilePath $python -ArgumentList $proxy `
            -WindowStyle Hidden -PassThru
        Write-Host "LCU proxy PID $($proxyProc.Id)"
    }

    $watcher.WaitForChanged([System.IO.WatcherChangeTypes]::Created, 5000) | Out-Null
    if (Test-Path $lockfile) {
        Write-Host "League started — launching roastbot"
        & docker compose -f "$botDir\docker-compose.yml" up -d
    }
}
