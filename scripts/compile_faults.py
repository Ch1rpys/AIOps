"""枚举配方、写出 jsonl、冻结 holdout。可选：在影子世界校准。

见 docs/影子世界与故障编译器计划.md §3.4 / §3.5 / P2。
校准需要 Docker 与已拉起的 aiops-twin；默认只做枚举与冻结。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description="枚举故障配方并冻结 holdout")
    parser.add_argument("--calibrate", action="store_true", help="在影子世界校准（需要 Docker）")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="校准时最多处理的配方数，0 表示全部合格候选",
    )
    args = parser.parse_args()

    from app.services.compiler.canonical import canonical_recipes
    from app.services.compiler.enumerate import enumerate_recipes
    from app.services.compiler.split import write_generated, write_holdout

    recipes = enumerate_recipes()
    write_generated(recipes)
    document = write_holdout(ids=None)
    recipes_dir = ROOT / "knowledge/compiler/recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    for recipe in canonical_recipes().values():
        path = recipes_dir / f"{recipe.canonical_scene}.json"
        path.write_text(
            json.dumps(recipe.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
    print(f"enumerated={len(recipes)} holdout={len(document['ids'])} version={document['compiler_version']}")
    if not args.calibrate:
        print("校准请用 scripts/calibrate_pool.py（单原语全覆盖 + 双原语至少 80）")
        return 0

    from app.config import Settings
    from app.integrations.docker import DockerController
    from app.integrations.probes import Probes
    from app.repositories.store import Store
    from app.services.compiler.calibrate import calibrate_recipe
    from app.services.compiler.split import freeze_ids
    from app.services.twin import TwinService
    from app.worlds import world_from_settings
    import httpx

    settings = Settings()
    store = Store(settings.data_dir / "platform.sqlite3")
    world = world_from_settings(settings, "twin")
    docker = DockerController(settings, world)
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        probes = Probes(settings, client, world)
        twin = TwinService(settings, docker, probes)
        holdout = set(freeze_ids(recipes))
        targets = [recipe for recipe in recipes if recipe.id in holdout or recipe.canonical_scene]
        if args.limit:
            targets = targets[: args.limit]
        ok = 0
        for recipe in targets:
            try:
                calibrated = calibrate_recipe(twin, recipe)
                store.upsert_recipe(calibrated)
                ok += 1
                print(f"calibrated {recipe.id}")
            except Exception as error:  # noqa: BLE001 - 校准失败逐条记录
                print(f"skip {recipe.id}: {error}")
        print(f"calibrated_ok={ok}/{len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
