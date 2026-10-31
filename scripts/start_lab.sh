#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker --context default compose --project-name aiops-demo --env-file .env --file deploy/docker-compose.yml up -d --build --wait demo-mysql demo-redis demo-app demo-nginx
