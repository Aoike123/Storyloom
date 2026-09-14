param([switch]$Dev)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (Test-Path -LiteralPath 'data\processes.json') {
    $existing = Get-Content -LiteralPath 'data\processes.json' -Raw | ConvertFrom-Json
    foreach ($item in $existing) {
        $running = Get-Process -Id $item.id -ErrorAction SilentlyContinue
        if ($running -and $running.StartTime.ToUniversalTime().Ticks -eq ([datetime]$item.started).ToUniversalTime().Ticks) {
            throw 'A recorded service is still running. Run scripts/stop.ps1 before restarting.'
        }
    }
}
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Please create .venv and install requirements.txt first. See README.md.' }
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$nodePath = if ($nodeCommand) { $nodeCommand.Source } else { Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' }
if (!(Test-Path -LiteralPath $nodePath)) { throw 'Node.js is required.' }
if (!(Test-Path -LiteralPath '.env.local')) { Copy-Item -LiteralPath '.env.example' -Destination '.env.local' }
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'data\logs') | Out-Null
& $pythonPath -m backend.media
if ($LASTEXITCODE -ne 0) { throw 'Demo media initialization failed.' }
$nextPath = Join-Path $projectRoot 'web\node_modules\next\dist\bin\next'
$mode = if ($Dev) { 'dev' } else { 'start' }
if (!$Dev -and !(Test-Path -LiteralPath 'web\.next\BUILD_ID')) { throw 'Build frontend first, or use -Dev.' }
$specs = @(
    @{ Name='api'; Exe=$pythonPath; Args=@('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8000'); Dir=$projectRoot },
    @{ Name='worker'; Exe=$pythonPath; Args=@('-m','backend.worker'); Dir=$projectRoot },
    @{ Name='web'; Exe=$nodePath; Args=@(('"' + $nextPath + '"'),$mode,'--hostname','127.0.0.1','--port','3000'); Dir=(Join-Path $projectRoot 'web') }
)
$processes = @()
foreach ($spec in $specs) {
    $logPath = Join-Path $projectRoot ('data\logs\' + $spec.Name)
    $process = Start-Process -FilePath $spec.Exe -ArgumentList $spec.Args -WorkingDirectory $spec.Dir -WindowStyle Hidden -PassThru -RedirectStandardOutput ($logPath + '.out.log') -RedirectStandardError ($logPath + '.err.log')
    $processes += @{ name=$spec.Name; id=$process.Id; started=$process.StartTime.ToUniversalTime().ToString('o') }
}
$processes | ConvertTo-Json | Set-Content -LiteralPath 'data\processes.json' -Encoding utf8
Write-Output 'Storyloom started: http://127.0.0.1:3000'
Write-Output 'Local configuration: .env.local ; logs: data/logs'
