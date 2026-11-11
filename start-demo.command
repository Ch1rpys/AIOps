#!/bin/bash
# macOS 双击本文件即可在本机启动演示。不连接虚拟机。
cd "$(dirname "$0")"
set -euo pipefail
root="$(pwd)"
uv_dir="$root/.tools/uv"
mkdir -p "$uv_dir"
if [[ ! -x "$uv_dir/uv" ]]; then
    echo "正在把 uv 装进 .tools"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$uv_dir" sh
fi
export UV_PYTHON_INSTALL_DIR="$root/.tools/python"
"$uv_dir/uv" python install 3.11
set +e
"$uv_dir/uv" run --frozen --python 3.11 python scripts/start_demo.py
status=$?
set -e
if [[ $status -ne 0 && $status -ne 130 ]]; then
    echo "FAIL exit $status"
    read -r -p "按回车关闭窗口..."
fi
exit "$status"
