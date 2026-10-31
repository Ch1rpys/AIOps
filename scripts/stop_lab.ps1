$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
docker --context default compose --project-name aiops-demo --env-file .env --file deploy/docker-compose.yml stop demo-mysql demo-redis demo-app demo-nginx holmes
exit $LASTEXITCODE
