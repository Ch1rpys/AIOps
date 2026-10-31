$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Output 'FAIL Docker CLI 未安装'; exit 1 }
docker --context default compose --project-name aiops-demo --env-file .env --file deploy/docker-compose.yml up -d --build --wait
exit $LASTEXITCODE
