"""seen / holdout 冻结。算法：已校准（或先验合格）非 canonical 非 negative，哈希字典序后 30%。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import ROOT, DomainError
from app.services.compiler import COMPILER_VERSION
from app.services.compiler.canonical import canonical_recipes
from app.services.compiler.enumerate import eligible_pool, enumerate_recipes
from app.services.compiler.schema import Recipe

HOLDOUT_PATH = ROOT / "knowledge/compiler/holdout.json"
HOLDOUT_APPENDIX_PATH = ROOT / "knowledge/compiler/holdout.enumerated-v1.json"
GENERATED_PATH = ROOT / "knowledge/compiler/generated.jsonl"
CALIBRATED_PATH = ROOT / "knowledge/compiler/calibrated.jsonl"
CANONICAL_EXCLUDED = [
    "MYSQL_DOWN",
    "REDIS_DOWN",
    "NGINX_502",
    "NGINX_UPSTREAM",
    "APP_HTTP_500",
    "DUAL_MYSQL_REDIS",
]


def freeze_ids(recipes: list[Recipe] | None = None) -> list[str]:
    """冻结算法：已校准、非 canonical、非 negative，哈希字典序后 30%。

    校准清单不足 80 条双原语时，回退到先验合格枚举池，避免 CI 在校准完成前崩掉。
    校准达标后只认 calibrated.jsonl，先验不再当真值。
    """
    calibrated = recipes if recipes is not None else load_calibrated()
    pool = calibrated_eligible(calibrated)
    dual = sum(1 for recipe in pool if len(recipe.primitives) == 2)
    if dual < 80:
        pool = eligible_pool(recipes)
    return _tail_ids(pool)


def freeze_calibrated_ids(recipes: list[Recipe] | None = None) -> list[str]:
    return _tail_ids(calibrated_eligible(recipes))


def _tail_ids(pool: list[Recipe]) -> list[str]:
    ordered = sorted(pool, key=lambda recipe: recipe.canonical_hash)
    if not ordered:
        return []
    cut = max(1, int(len(ordered) * 0.3))
    tail = ordered[-cut:] if cut < len(ordered) else ordered
    return [recipe.id for recipe in tail]


def calibrated_eligible(recipes: list[Recipe] | None = None) -> list[Recipe]:
    recipes = recipes if recipes is not None else load_calibrated()
    return [
        recipe
        for recipe in recipes
        if recipe.signature
        and recipe.signature.source == "calibrated"
        and recipe.signature.failing_checks
        and not recipe.negative
        and recipe.canonical_scene is None
        and not recipe.unsupported
        and not recipe.dirty
    ]


def load_calibrated(path: Path = CALIBRATED_PATH) -> list[Recipe]:
    from app.services.compiler.schema import parse_recipe

    if not path.exists():
        return []
    recipes: list[Recipe] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        recipes.append(parse_recipe(json.loads(line)))
    return recipes


def write_calibrated(recipes: list[Recipe], path: Path = CALIBRATED_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(recipe.model_dump(mode="json", exclude_none=True), ensure_ascii=False, sort_keys=True)
        for recipe in sorted(recipes, key=lambda item: item.canonical_hash)
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def holdout_document(ids: list[str] | None = None) -> dict:
    return {
        "frozen_at": datetime.now(UTC).isoformat(),
        "compiler_version": COMPILER_VERSION,
        "algorithm": "hash_sort_tail_30_percent",
        "canonical_excluded": CANONICAL_EXCLUDED,
        "ids": ids if ids is not None else freeze_ids(),
    }


def load_holdout(path: Path = HOLDOUT_PATH) -> dict:
    if not path.exists():
        raise DomainError("COMPILER_VERSION_MISMATCH", "holdout 冻结文件不存在", 503)
    return json.loads(path.read_text(encoding="utf-8"))


def write_holdout(path: Path = HOLDOUT_PATH, ids: list[str] | None = None) -> dict:
    document = holdout_document(ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


def write_generated(recipes: list[Recipe], path: Path = GENERATED_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "id": recipe.id,
                "canonical_hash": recipe.canonical_hash,
                "title": recipe.title,
                "primitives": [item.model_dump(mode="json", exclude_none=True) for item in recipe.primitives],
                "recoverable": recipe.recoverable,
                "negative": recipe.negative,
                "poison": recipe.poison,
                "canonical_scene": recipe.canonical_scene,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for recipe in recipes
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_holdout(path: Path = HOLDOUT_PATH) -> dict:
    document = load_holdout(path)
    if int(document.get("compiler_version") or 0) != COMPILER_VERSION:
        raise DomainError("COMPILER_VERSION_MISMATCH", "holdout 与当前枚举器不对版", 409)
    expected = freeze_ids()
    actual = list(document.get("ids") or [])
    if actual != expected:
        raise DomainError("COMPILER_VERSION_MISMATCH", "holdout 哈希集合与当前枚举不一致", 409)
    canonical_ids = {recipe.id for recipe in canonical_recipes().values()}
    overlap = set(actual) & canonical_ids
    if overlap:
        raise DomainError("COMPILER_VERSION_MISMATCH", "holdout 不得包含规范场景", 409)
    dual = 0
    by_id = {recipe.id: recipe for recipe in enumerate_recipes()}
    by_id.update({recipe.id: recipe for recipe in load_calibrated()})
    for recipe_id in actual:
        recipe = by_id.get(recipe_id)
        if recipe and len(recipe.primitives) == 2:
            dual += 1
    if dual < 20:
        raise DomainError("COMPILER_VERSION_MISMATCH", "holdout 双原语不足 20 条", 409)
    pool = calibrated_eligible()
    if sum(1 for recipe in pool if len(recipe.primitives) == 2) < 80:
        pool = eligible_pool()
    if len(actual) < max(1, int(len(pool) * 0.3)):
        raise DomainError("COMPILER_VERSION_MISMATCH", "holdout 不足总池 30%", 409)
    return {"ok": True, "size": len(actual), "dual": dual}


def is_holdout(recipe_id: str, path: Path = HOLDOUT_PATH) -> bool:
    try:
        return recipe_id in set(load_holdout(path).get("ids") or [])
    except DomainError:
        return recipe_id in set(freeze_ids())


def _holdout_id_set() -> set[str]:
    try:
        return set(load_holdout().get("ids") or [])
    except DomainError:
        return set()


def enumerated_size(path: Path = GENERATED_PATH) -> int | None:
    if not path.exists():
        return None
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def list_generated_summaries(
    holdout: bool | None = None, limit: int = 50, offset: int = 0, path: Path = GENERATED_PATH
) -> list[dict]:
    """库为空时，编译器页仍能从冻结 jsonl 列出编号与标题（不含原语）。"""
    if not path.exists():
        return []
    hidden = _holdout_id_set()
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        marked = data.get("id") in hidden
        if holdout is True and not marked:
            continue
        if holdout is False and marked:
            continue
        rows.append(
            {
                "id": data.get("id"),
                "title": data.get("title"),
                "holdout": marked,
                "canonical_scene": data.get("canonical_scene"),
                "calibrated_at": None,
                "compiler_version": COMPILER_VERSION,
            }
        )
    return rows[offset : offset + limit]


def recipe_body_from_generated(recipe_id: str, path: Path = GENERATED_PATH) -> dict | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("id") == recipe_id:
            return data
    return None
