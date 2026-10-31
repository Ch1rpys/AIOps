param([switch]$Preflight)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$arguments = @('scripts/acceptance_mysql.py')
if ($Preflight) { $arguments += '--preflight' }
& ./.venv/Scripts/python.exe @arguments
exit $LASTEXITCODE
