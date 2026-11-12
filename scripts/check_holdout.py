"""CI：holdout 哈希未变、不与 canonical 重叠、双原语不少于 20。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    from app.services.compiler.split import check_holdout

    result = check_holdout()
    print(f"holdout ok size={result['size']} dual={result['dual']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
