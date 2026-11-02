param([switch]$Preflight, [string[]]$Scenario, [int]$Rounds = 1)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$python = if (Test-Path './.venv/Scripts/python.exe') { './.venv/Scripts/python.exe' } else { './.venv/bin/python' }
$arguments = @('scripts/run_acceptance.py')
if ($Preflight) { $arguments += '--preflight' }
if ($Scenario) { $arguments += '--scenario'; $arguments += $Scenario }
if ($Rounds -gt 0) { $arguments += '--rounds'; $arguments += $Rounds }
& $python @arguments
exit $LASTEXITCODE
