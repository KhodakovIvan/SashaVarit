# Остановка процесса бота (python с run.py из этой установки).

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine.Contains("run.py") -and
        $_.CommandLine.Contains($Root)
    }

if (-not $procs) {
    Write-Host "Бот не найден среди процессов python."
    exit 0
}

foreach ($p in $procs) {
    Write-Host "Останавливаю PID $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force
}
Write-Host "Готово."
