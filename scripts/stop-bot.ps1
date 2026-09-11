# Stop SashaVarit bot process (python running run.py from this install).

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine.Contains("run.py") -and
        $_.CommandLine.Contains($Root)
    }

if (-not $procs) {
    Write-Host "Bot process not found."
    exit 0
}

foreach ($p in $procs) {
    Write-Host "Stopping PID $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force
}
Write-Host "Done."