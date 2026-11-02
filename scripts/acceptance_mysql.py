"""兼容入口：旧的 acceptance_mysql.py 调用转发到参数化的 run_acceptance.py。

保留是为了不打断既有调用习惯；新脚本支持 --scenario 与 --rounds。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_acceptance import main  # noqa: E402

if __name__ == "__main__":
    # 旧脚本只覆盖 MYSQL_DOWN 单轮；新脚本可指定场景与轮次。
    sys.argv = [sys.argv[0], "--scenario", "MYSQL_DOWN", *sys.argv[1:]]
    sys.exit(main())
