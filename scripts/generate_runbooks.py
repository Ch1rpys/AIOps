"""从 knowledge/topology.yaml 生成通用 Runbook。

通用 Runbook 与影响图共用同一份拓扑数据，避免两处各写一份、互相漂移。
生成后请提交产物；测试会校验产物与拓扑一致。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.topology import RUNBOOK_PATH, render_general_runbook  # noqa: E402


def main() -> int:
    content = render_general_runbook()
    RUNBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNBOOK_PATH.write_text(content, encoding="utf-8")
    print(f"已生成 {RUNBOOK_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
