param(
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Python = Join-Path $Root ".venv\Scripts\python.exe"

Set-Location $Root

function Get-ListenerProcess {
    param([int]$LocalPort)

    $connection = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $connection) {
        return $null
    }

    Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"
}

$listener = Get-ListenerProcess -LocalPort $Port
if ($listener) {
    $commandLine = [string]$listener.CommandLine
    if ($commandLine -like "*$Root*" -and $commandLine -like "*uvicorn*" -and $commandLine -like "*app.main:app*") {
        Write-Host "Service is already running at http://$HostName`:$Port"
        exit 0
    }

    Write-Error "Port $Port is already used by PID $($listener.ProcessId): $commandLine"
}

if (-not (Test-Path $Python)) {
    Write-Host "Virtual environment not found. Creating .venv..."
    python -m venv .venv
}

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
}

Write-Host "Installing/updating project dependencies..."
& $Python -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Starting service at http://$HostName`:$Port"
Write-Host "Keep this window open. Press Ctrl+C to stop, or run stop.bat from another window."
& $Python -m uvicorn app.main:app --host $HostName --port $Port
