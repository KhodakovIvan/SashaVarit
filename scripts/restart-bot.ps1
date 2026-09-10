# Перезапуск бота: stop + start в скрытом окне PowerShell.

$ErrorActionPreference = "Stop"
$Start = Join-Path $PSScriptRoot "start-bot.ps1"
$Stop = Join-Path $PSScriptRoot "stop-bot.ps1"

& $Stop
Start-Sleep -Seconds 2
Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $Start
) -WindowStyle Hidden

Write-Host "Бот перезапускается в фоне. Лог: $(Join-Path (Split-Path $PSScriptRoot -Parent) 'logs\bot.log')"
