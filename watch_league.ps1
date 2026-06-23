$lockfile = "C:\Riot Games\League of Legends\lockfile"
$botDir   = $PSScriptRoot

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path   = Split-Path $lockfile
$watcher.Filter = Split-Path $lockfile -Leaf
$watcher.EnableRaisingEvents = $true

Write-Host "Watching for League client..."

while ($true) {
    $watcher.WaitForChanged([System.IO.WatcherChangeTypes]::Created, [System.Int32]::MaxValue) | Out-Null
    Write-Host "League started — launching roastbot"
    & docker compose -f "$botDir\docker-compose.yml" up -d
}
