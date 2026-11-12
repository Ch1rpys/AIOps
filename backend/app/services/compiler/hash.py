import hashlib
import json

from app.services.compiler.schema import Primitive, Recipe, RecipeBody


def canonical_primitives_json(primitives: list[Primitive] | list[dict]) -> str:
    """对 primitives 做 JSON canonicalization：键排序、无空白差异。title 不进哈希。"""
    payload = []
    for item in primitives:
        data = item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else dict(item)
        payload.append(data)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(primitives: list[Primitive] | list[dict]) -> str:
    return hashlib.sha256(canonical_primitives_json(primitives).encode("utf-8")).hexdigest()


def recipe_id_for(digest: str) -> str:
    return f"rec_{digest[:12]}"


def identify(primitives: list[Primitive] | list[dict]) -> tuple[str, str]:
    digest = canonical_hash(primitives)
    return recipe_id_for(digest), digest


def finalize_recipe(body: RecipeBody, **extra) -> Recipe:
    recipe_id, digest = identify(body.primitives)
    return Recipe(
        schema_version=body.schema_version,
        id=recipe_id,
        title=body.title,
        primitives=body.primitives,
        recoverable=body.recoverable,
        negative=body.negative,
        poison=body.poison,
        canonical_scene=body.canonical_scene,
        preferred_actions=list(body.preferred_actions),
        canonical_hash=digest,
        **extra,
    )
