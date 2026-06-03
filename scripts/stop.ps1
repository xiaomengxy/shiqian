param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir

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
if (-not $listener) {
    Write-Host "No service is listening on port $Port."
    exit 0
}

$commandLine = [string]$listener.CommandLine
if (-not ($commandLine -like "*$Root*" -and $commandLine -like "*uvicorn*" -and $commandLine -like "*app.main:app*")) {
    Write-Error "Port $Port is used by another process. Refusing to stop PID $($listener.ProcessId): $commandLine"
}

Write-Host "Stopping service on port $Port, PID $($listener.ProcessId)..."
Stop-Process -Id $listener.ProcessId -Force
Write-Host "Stopped."
