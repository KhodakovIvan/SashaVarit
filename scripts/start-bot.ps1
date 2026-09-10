# Запуск бота SashaVarit с автоперезапуском при падении.
# Лог: <корень>/logs/bot.log
# Автозапуск: Планировщик заданий → powershell.exe -NoProfile -ExecutionPolicy Bypass -File "...\scripts\start-bot.ps1"

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$RunPy = Join-Path $Root "run.py"
$LogDir = Join-Path $Root "logs"
$Log = Join-Path $LogDir "bot.log"

if (-not (Test-Path $Python)) {
    throw "Нет $Python — создайте .venv и выполните: .\.venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path $RunPy)) {
    throw "Нет $RunPy"
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
    Write-Host "Бот уже запущен (PID $($busy.ProcessId -join ', ')). Выход."
    exit 0
}

Write-Host "Старт бота из $Root (лог: $Log). Ctrl+C в этом окне остановит цикл перезапуска."
while ($true) {
    "$(Get-Date -Format o) START" | Add-Content -Path $Log -Encoding utf8
    & $Python $RunPy *>> $Log
    $code = $LASTEXITCODE
    "$(Get-Date -Format o) EXIT $code, restart in 5s" | Add-Content -Path $Log -Encoding utf8
    Start-Sleep -Seconds 5
}
