"""抽 holdout 跑未见故障盲诊。默认 10 条 × 指定臂。需要平台已启动。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="对 holdout 抽样做未见盲诊")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--arms", default="A")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.environ.get("ADMIN_TOKEN", "")
    headers = {"X-Admin-Token": token} if token else {}
    holdout = json.loads((ROOT / "knowledge/compiler/holdout.json").read_text(encoding="utf-8"))
    all_ids = list(holdout.get("ids") or [])
    if not all_ids:
        print("holdout 为空", file=sys.stderr)
        return 1
    arms = [item.strip() for item in args.arms.split(",") if item.strip()]
    calibrated_ids = set()
    calibrated_path = ROOT / "knowledge/compiler/calibrated.jsonl"
    if calibrated_path.exists():
        for line in calibrated_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                calibrated_ids.add(json.loads(line).get("id"))
    ids = [item for item in all_ids if item in calibrated_ids][: args.n] if calibrated_ids else all_ids[: args.n]
    if not ids:
        print("没有已校准的 holdout 可评", file=sys.stderr)
        return 1
    with httpx.Client(timeout=300.0) as client:
        for recipe_id in ids:
            key = "EVAL-" + uuid.uuid4().hex[:16]
            response = client.post(
                f"{args.base}/api/evaluation/runs",
                headers={**headers, "Idempotency-Key": key},
                json={
                    "cohort": "unseen",
                    "recipe_id": recipe_id,
                    "arms": arms,
                    "confirm": True,
                },
            )
            print(recipe_id, response.status_code, response.text[:200])
            if response.status_code >= 400:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
