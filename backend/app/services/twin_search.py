"""R 臂：影子里按目录穷尽/启发式搜索能把签名修回去的动作。不计模型正确率。"""

from app.config import CHECK_IDS, DomainError, Settings
from app.scenarios import SCENARIOS
from app.schemas.domain import Scenario, now
from app.services.action_catalog import CATALOG
from app.services.compiler.schema import Recipe
from app.services.twin import TwinService

CANONICAL_RECOVERY = (
    "START_DEMO_MYSQL",
    "START_DEMO_REDIS",
    "START_DEMO_APP",
    "RESTORE_NGINX_UPSTREAM",
    "RESTORE_APP_MODE",
    "MYSQL_RESET_DEMO_PASSWORD",
)


def candidate_actions(recipe: Recipe, limit: int = 30) -> list[str]:
    """禁止扫描全部 THEN 变体。THEN 只保留「动作 + 签名内探针」。"""
    affected = set((recipe.signature.affected_services if recipe.signature else None) or [])
    failing = list((recipe.signature.failing_checks if recipe.signature else None) or [])
    if not affected:
        from app.services.compiler.primitives import prior_affected_services

        affected = set(prior_affected_services(recipe.primitives))
    if not failing:
        from app.services.compiler.primitives import prior_failing_checks

        failing = sorted(prior_failing_checks(recipe.primitives))
    bases: list[str] = []
    for action_id, entry in CATALOG.items():
        if "__THEN__" in action_id:
            continue
        if entry.kind == "none":
            continue
        if action_id in CANONICAL_RECOVERY or entry.target in affected:
            bases.append(action_id)
    unique: list[str] = []
    for action_id in [*CANONICAL_RECOVERY, *bases]:
        if action_id not in unique and action_id in CATALOG:
            unique.append(action_id)
    selected = unique[:limit]
    chained: list[str] = []
    for action_id in selected:
        chained.append(action_id)
        for probe in failing:
            then_id = f"{action_id}__THEN__{probe.upper()}"
            if then_id in CATALOG and then_id not in chained:
                chained.append(then_id)
    return chained[:limit]


def search(twin: TwinService, recipe: Recipe, limit: int | None = None) -> dict:
    settings: Settings = twin.settings
    cap = limit or settings.r_arm_max_candidates
    if not twin.available() and not twin._try_ensure():
        raise DomainError("TWIN_UNAVAILABLE", "影子不可用，无法搜索", 503)
    candidates = candidate_actions(recipe, cap)
    ranked = []
    trials = 0
    for action_id in candidates:
        result = twin.trial(recipe, action_id)
        trials += 1
        if result.get("skipped"):
            continue
        after = result.get("after") or {}
        remaining = sum(1 for name in CHECK_IDS if after.get(name) == "FAIL")
        ranked.append(
            {
                "action_id": action_id,
                "would_resolve_signature": bool(result.get("would_resolve_signature")),
                "remaining_fail": remaining,
                "predicted_pass": result.get("predicted_pass") or [],
                "elapsed_ms": result.get("elapsed_ms"),
            }
        )
        if result.get("would_resolve_signature"):
            break
    ranked.sort(key=lambda item: (not item["would_resolve_signature"], item["remaining_fail"]))
    top = ranked[:3]
    payload = {
        "recipe_id": recipe.id,
        "candidates": top,
        "tried": trials,
        "created_at": now(),
    }
    if twin.store is not None:
        with twin.store.session() as session, session.begin():
            twin.store.audit(session, None, "twin_search", payload)
    return payload
