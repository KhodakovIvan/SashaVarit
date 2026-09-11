# Start SashaVarit bot with auto-restart on exit.
# Log: <repo>/logs/bot.log
# Autostart: Task Scheduler -> powershell.exe -NoProfile -ExecutionPolicy Bypass -File "...\scripts\start-bot.ps1"
#
# Python logging goes to stderr; do not treat that as a terminating PowerShell error.

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$RunPy = Join-Path $Root "run.py"
$LogDir = Join-Path $Root "logs"
$Log = Join-Path $LogDir "bot.log"

if (-not (Test-Path $Python)) {
    throw "Missing $Python - create .venv and run: .\.venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path $RunPy)) {
    throw "Missing $RunPy"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $Root

$busy = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine.Contains("run.py") -and
        $_.CommandLine.Contains($Root)
    }
if ($busy) {
    Write-Host "Bot already running (PID $($busy.ProcessId -join ', ')). Exit."
    exit 0
}

Write-Host "Starting bot from $Root (log: $Log). Ctrl+C stops the restart loop."
while ($true) {
    "$(Get-Date -Format o) START" | Add-Content -Path $Log -Encoding utf8
    # cmd redirect keeps Python stderr in the log without NativeCommandError
    $cmd = "`"$Python`" `"$RunPy`" >> `"$Log`" 2>&1"
    cmd.exe /c $cmd
    $code = $LASTEXITCODE
    "$(Get-Date -Format o) EXIT $code, restart in 5s" | Add-Content -Path $Log -Encoding utf8
    Start-Sleep -Seconds 5
}