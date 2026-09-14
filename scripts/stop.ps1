$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$recordPath = Join-Path $projectRoot 'data\processes.json'
if (!(Test-Path -LiteralPath $recordPath)) { Write-Output 'No recorded processes.'; exit }
$items = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
foreach ($item in $items) {
    $process = Get-Process -Id $item.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq ([datetime]$item.started).ToUniversalTime().Ticks) {
        & taskkill /PID $process.Id /T /F | Out-Null
        Write-Output ('Stopped ' + $item.name)
    }
}
