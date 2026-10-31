#!/usr/bin/env bash
set -euo pipefail
trap 'echo "FAIL startup at line $LINENO; inspect data/backend.log and data/frontend.log" >&2' ERR
export PATH="$HOME/.local/bin:$PATH"
[[ "$(uname -s)" == Linux ]] || { echo 'FAIL run inside Ubuntu; macOS uses scripts/start_mac.sh' >&2; exit 1; }
command -v flock >/dev/null || { echo 'FAIL missing dependency: flock (util-linux)' >&2; exit 1; }
cd "${1:-$(dirname "$0")/..}"
mkdir -p data
exec 9>data/startup.lock
flock -n 9 || { echo 'FAIL another startup is running' >&2; exit 1; }
for command in uv node npm docker curl; do
    command -v "$command" >/dev/null || { echo "FAIL missing dependency: $command" >&2; exit 1; }
done
[[ -f .env ]] || { echo 'FAIL missing .env; copy .env.example and configure it first' >&2; exit 1; }
docker --context default info >/dev/null
compose=(docker --context default compose --project-name aiops-demo --env-file .env --file deploy/docker-compose.yml)
"${compose[@]}" config --quiet
uv sync --frozen
platform_options=()
if [[ -n "${2:-}" ]]; then platform_options=(--target-platform "$2"); fi
.venv/bin/python scripts/check_platform.py "${platform_options[@]}"
if [[ ! -f frontend/node_modules/vite/bin/vite.js || frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]]; then
    (cd frontend && npm ci --no-audit --no-fund)
fi

# 已存在的 demo 可能正在注入故障；启动入口不能绕过审批恢复它们。
existing=$("${compose[@]}" ps --all --quiet demo-mysql demo-redis demo-app demo-nginx)
if [[ -z "$existing" ]]; then
    "${compose[@]}" up -d --build --wait --wait-timeout 180 demo-mysql demo-redis demo-app demo-nginx
else
    echo 'INFO preserving existing demo container states; use the confirmed UI reset if needed'
fi
"${compose[@]}" up -d --wait --wait-timeout 120 holmes
.venv/bin/python scripts/check_platform.py "${platform_options[@]}" --containers

port_in_use() {
    .venv/bin/python - "$1" <<'PY'
import socket
import sys
with socket.socket() as client:
    sys.exit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}
wait_for() {
    local url=$1
    for ((attempt=0; attempt<30; attempt++)); do
        if curl --fail --silent --max-time 2 "$url" >/dev/null; then return 0; fi
        sleep 1
    done
    echo "FAIL service did not become ready: $url" >&2
    return 1
}

if port_in_use 8000; then
    curl -fsS --max-time 3 http://127.0.0.1:8000/api/scenarios |
        .venv/bin/python -c 'import json,sys; assert "MYSQL_DOWN" in [s["key"] for s in json.load(sys.stdin)]'
    echo 'PASS reusing backend on 8000'
else
    nohup .venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1 >data/backend.log 2>&1 </dev/null 9>&- &
    echo $! >data/backend.pid
    wait_for http://127.0.0.1:8000/api/scenarios
fi
if port_in_use 5173; then
    curl -fsS --max-time 3 http://127.0.0.1:5173/@vite/client >/dev/null
    echo 'PASS reusing frontend on 5173'
else
    (cd frontend && exec nohup node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort) >data/frontend.log 2>&1 </dev/null 9>&- &
    echo $! >data/frontend.pid
    wait_for http://127.0.0.1:5173/@vite/client
fi
curl -fsS --max-time 30 http://127.0.0.1:8000/api/health |
    .venv/bin/python -c '
import json, sys
h = json.load(sys.stdin)
assert h["platform"] == "PASS" and h["mode"] == "REAL", "Expected REAL platform"
print("PASS platform REAL; Holmes=" + h["holmes"]["status"])
print("INFO lab healthy=" + str(h["lab"]["healthy"]) + "; active incident=" + str(h["lab"]["active_incident_id"]))
print("INFO planner configured=" + str(h["planner"]["configured"]) + "; model calls require real diagnosis")
if h["holmes"]["status"] != "PASS" or not h["planner"]["configured"] or not h["admin_configured"]:
    sys.exit("FAIL Holmes / planner / admin configuration is not ready")
if not h["lab"]["healthy"]:
    print("WARN lab is not healthy; preserved fault state, inspect the lab page")
'
echo 'PASS startup ready: http://127.0.0.1:5173'
