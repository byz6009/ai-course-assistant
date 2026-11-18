param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonExe = $env:KEJI_PYTHON
if (-not $pythonExe) {
    foreach ($name in @('python', 'python3')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($candidate) {
            & $candidate.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonExe = $candidate.Source; break }
        }
    }
}
if (-not $pythonExe) {
    $bundles = Get-ChildItem -LiteralPath 'C:\Program Files\WindowsApps' -Directory -Filter 'OpenAI.CodexPrimaryRuntime.*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    foreach ($bundle in $bundles) {
        $candidate = Join-Path $bundle.FullName 'dependencies\python\python.exe'
        if (Test-Path -LiteralPath $candidate) { $pythonExe = $candidate; break }
    }
}
if (-not $pythonExe) { throw 'Python 3.10+ is required. Set KEJI_PYTHON to its full path.' }
if (-not (Test-Path -LiteralPath 'dist\index.html')) { throw 'Frontend build missing. Run npm ci and npm run build first.' }
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    if ($health.mode -eq 'local-single-user') { Write-Host "Keji is already running: http://127.0.0.1:$Port"; exit 0 }
} catch { }
New-Item -ItemType Directory -Force -Path '.runtime' | Out-Null
$process = Start-Process -FilePath $pythonExe -ArgumentList @('server/app.py', '--port', $Port) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput '.runtime/server.log' -RedirectStandardError '.runtime/server-error.log'
$process.Id | Set-Content -LiteralPath '.runtime/server.pid'
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 200
    if ($process.HasExited) { throw 'Server stopped. See .runtime/server-error.log.' }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
        if ($health.status -eq 'ok') { Write-Host "Keji is ready: http://127.0.0.1:$Port"; exit 0 }
    } catch { }
}
throw 'Server did not become ready. Check .runtime/server-error.log.'
