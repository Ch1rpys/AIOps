"""在影子世界校准配方：单原语全覆盖尝试 + 至少 80 条双原语。

真值只来自影子探针。见 docs/影子世界与故障编译器计划.md §3.5 / §4.5。
可重复执行：已写入 calibrated.jsonl 的编号会跳过。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _kind_key(recipe) -> str:
    return "+".join(sorted(item.kind for item in recipe.primitives))


def _targets():
    from app.services.compiler.canonical import canonical_recipes
    from app.services.compiler.enumerate import eligible_pool, enumerate_recipes

    seen: dict[str, object] = {}
    ordered = []

    def add(recipe) -> None:
        if recipe.id in seen:
            return
        seen[recipe.id] = recipe
        ordered.append(recipe)

    for recipe in canonical_recipes().values():
        add(recipe)
    eligible = eligible_pool()

    def _has_stop(recipe) -> bool:
        return any(item.kind == "life" and item.verb in {"stop", "kill", "pause"} for item in recipe.primitives)

    duals = [recipe for recipe in eligible if len(recipe.primitives) == 2]
    duals.sort(key=lambda recipe: (0 if _has_stop(recipe) else 1, _kind_key(recipe), recipe.canonical_hash))
    for recipe in duals:
        add(recipe)
    for recipe in eligible:
        if len(recipe.primitives) == 1:
            add(recipe)
    for recipe in enumerate_recipes():
        if len(recipe.primitives) == 1:
            add(recipe)
    return ordered


def main() -> int:
    from app.config import DomainError, Settings
    from app.integrations.docker import DockerController
    from app.integrations.probes import Probes
    from app.repositories.store import Store
    from app.services.compiler.calibrate import calibrate_recipe
    from app.services.compiler.split import CALIBRATED_PATH, load_calibrated, write_calibrated
    from app.services.twin import TwinService
    from app.worlds import world_from_settings
    import httpx

    settings = Settings()
    store = Store(settings.data_dir / "platform.sqlite3")
    world = world_from_settings(settings, "twin")
    docker = DockerController(settings, world)
    done = {recipe.id: recipe for recipe in load_calibrated()}
    dual_ok = sum(1 for recipe in done.values() if len(recipe.primitives) == 2 and recipe.canonical_scene is None)
    single_ok = sum(1 for recipe in done.values() if len(recipe.primitives) == 1)
    print(
        f"resume calibrated={len(done)} dual={dual_ok} single={single_ok} file={CALIBRATED_PATH}",
        flush=True,
    )
    targets = _targets()
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        probes = Probes(settings, client, world)
        twin = TwinService(settings, docker, probes)
        twin.ensure()
        if not twin.baseline():
            twin.reset_clean()
        if not twin.baseline():
            print("FAIL twin baseline", flush=True)
            return 1
        for recipe in targets:
            if recipe.id in done:
                continue
            if dual_ok >= 80 and len(recipe.primitives) == 2 and recipe.canonical_scene is None:
                # 双原语已够；继续把剩余单原语跑完。
                continue
            try:
                calibrated = calibrate_recipe(twin, recipe)
            except DomainError as error:
                print(f"skip {recipe.id} {error.code} {error.message}", flush=True)
                if error.code in {"TWIN_UNAVAILABLE", "CALIBRATION_DIRTY"}:
                    try:
                        twin.reset_clean()
                        twin.ensure()
                    except DomainError as recover:
                        print(f"FAIL recover {recover.code} {recover.message}", flush=True)
                        return 1
                    if not twin.baseline():
                        print("FAIL twin baseline after recover", flush=True)
                        return 1
                continue
            except Exception as error:  # noqa: BLE001
                print(f"skip {recipe.id} {type(error).__name__} {error}", flush=True)
                try:
                    twin.reset_clean()
                except Exception:
                    pass
                continue
            done[calibrated.id] = calibrated
            store.upsert_recipe(calibrated)
            write_calibrated(list(done.values()))
            if calibrated.canonical_scene is None and len(calibrated.primitives) == 2:
                dual_ok += 1
            if len(calibrated.primitives) == 1:
                single_ok += 1
            print(
                f"calibrated {calibrated.id} dual_ok={dual_ok} single_ok={single_ok} "
                f"failing={calibrated.signature.failing_checks if calibrated.signature else []}",
                flush=True,
            )
            if dual_ok >= 80:
                _freeze_from_calibrated()
    _freeze_from_calibrated()
    print(
        f"done calibrated={len(done)} dual_ok={dual_ok} single_ok={single_ok}",
        flush=True,
    )
    return 0 if dual_ok >= 80 else 1


def _freeze_from_calibrated() -> None:
    from app.services.compiler.split import (
        HOLDOUT_APPENDIX_PATH,
        HOLDOUT_PATH,
        freeze_calibrated_ids,
        write_holdout,
    )

    if HOLDOUT_PATH.exists() and not HOLDOUT_APPENDIX_PATH.exists():
        HOLDOUT_APPENDIX_PATH.write_text(HOLDOUT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    ids = freeze_calibrated_ids()
    if len(ids) < 20:
        return
    write_holdout(ids=ids)
    print(f"froze holdout size={len(ids)}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
