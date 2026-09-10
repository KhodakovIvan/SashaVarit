# Restart bot: stop, then start in a hidden PowerShell window.

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

$Log = Join-Path (Split-Path $PSScriptRoot -Parent) "logs\bot.log"
Write-Host "Bot restarting in background. Log: $Log"