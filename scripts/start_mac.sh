#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 1 && "$1" == --help ]]; then
    echo 'Usage: bash scripts/start_mac.sh <SSH-host-alias> </absolute/Ubuntu/project> [--no-browser]'
    exit 0
fi
if [[ $# -lt 2 || $# -gt 3 || ( $# -eq 3 && "$3" != --no-browser ) ]]; then
    echo 'FAIL usage: bash scripts/start_mac.sh <SSH-host-alias> </absolute/Ubuntu/project> [--no-browser]' >&2
    exit 1
fi
[[ "$(uname -s)" == Darwin ]] || { echo 'FAIL run this launcher on macOS' >&2; exit 1; }
# Rosetta 终端可能报告 x86_64；以硬件能力优先判断 Apple Silicon。
if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" == 1 ]]; then
    target=linux/arm64
else
    case "$(uname -m)" in
        arm64) target=linux/arm64 ;;
        x86_64) target=linux/amd64 ;;
        *) echo 'FAIL unsupported Mac architecture' >&2; exit 1 ;;
    esac
fi
destination=$1
remote_directory=$2
[[ "$destination" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ && "$remote_directory" =~ ^/[a-zA-Z0-9_./-]+$ ]] || {
    echo 'FAIL invalid SSH alias or Ubuntu directory' >&2
    exit 1
}
for command in ssh curl; do
    command -v "$command" >/dev/null || { echo "FAIL missing dependency: $command" >&2; exit 1; }
done
ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10)
echo "INFO starting Ubuntu services for $target; existing experiments are preserved"
ssh "${ssh_options[@]}" "$destination" "cd '$remote_directory' && bash scripts/start_all.sh '$remote_directory' '$target'"

tunnel_pid=''
cleanup() {
    if [[ -n "$tunnel_pid" ]]; then
        kill "$tunnel_pid" 2>/dev/null || true
        wait "$tunnel_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ssh "${ssh_options[@]}" -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -L 127.0.0.1:5173:127.0.0.1:5173 -L 127.0.0.1:8000:127.0.0.1:8000 "$destination" &
tunnel_pid=$!
ready=false
for ((attempt=0; attempt<20; attempt++)); do
    kill -0 "$tunnel_pid" 2>/dev/null || { echo 'FAIL SSH tunnel exited; check local port conflicts' >&2; exit 1; }
    if curl -fsS --max-time 2 http://127.0.0.1:8000/api/scenarios >/dev/null && \
        curl -fsS --max-time 2 http://127.0.0.1:5173/@vite/client >/dev/null && \
        kill -0 "$tunnel_pid" 2>/dev/null; then
        ready=true
        break
    fi
    sleep 1
done
[[ "$ready" == true ]] || { echo 'FAIL SSH tunnel readiness timed out' >&2; exit 1; }
echo 'PASS http://127.0.0.1:5173 (API: http://127.0.0.1:8000/docs)'
echo 'INFO keep this terminal open; Ctrl+C closes this tunnel and preserves Ubuntu services'
if [[ "${3:-}" != --no-browser ]]; then open http://127.0.0.1:5173; fi
if ! wait "$tunnel_pid"; then
    echo 'FAIL SSH tunnel disconnected' >&2
    exit 1
fi
