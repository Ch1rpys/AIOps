#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../frontend"
npm ci --no-audit --no-fund
exec npm run dev
