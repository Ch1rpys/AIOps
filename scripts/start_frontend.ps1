$ErrorActionPreference = 'Stop'
Set-Location (Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend')
npm ci --cache ../.cache/npm --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run dev
exit $LASTEXITCODE
