"""六个规范场景就是编译器也能生成的特例。"""

from app.schemas.domain import Scenario
from app.services.compiler.hash import finalize_recipe
from app.services.compiler.primitives import preferred_recovery_actions
from app.services.compiler.schema import Primitive, Recipe, RecipeBody
from app.services.compiler.titles import title_from_prior

_SPECS: dict[Scenario, tuple[str, list[dict]]] = {
    Scenario.MYSQL_DOWN: (
        "数据库探针失败",
        [{"kind": "life", "service": "demo-mysql", "verb": "stop"}],
    ),
    Scenario.REDIS_DOWN: (
        "缓存探针失败",
        [{"kind": "life", "service": "demo-redis", "verb": "stop"}],
    ),
    Scenario.NGINX_502: (
        "代理探针失败",
        [{"kind": "life", "service": "demo-app", "verb": "stop"}],
    ),
    Scenario.NGINX_UPSTREAM: (
        "代理探针失败",
        [{"kind": "nginx_upstream", "profile": "misdirected"}],
    ),
    Scenario.APP_HTTP_500: (
        "代理探针失败",
        [{"kind": "app_mode", "mode": "http_500"}],
    ),
    Scenario.DUAL_MYSQL_REDIS: (
        "数据库探针失败且缓存探针失败",
        [
            {"kind": "life", "service": "demo-mysql", "verb": "stop"},
            {"kind": "life", "service": "demo-redis", "verb": "stop"},
        ],
    ),
}


def canonical_recipe(scenario: Scenario) -> Recipe:
    title, raw = _SPECS[scenario]
    primitives = [Primitive.model_validate(item) for item in raw]
    recoverable = scenario != Scenario.APP_HTTP_500
    body = RecipeBody(
        title=title or title_from_prior(primitives),
        primitives=primitives,
        recoverable=recoverable,
        negative=False,
        poison=False,
        canonical_scene=scenario,
        preferred_actions=preferred_recovery_actions(primitives),
    )
    return finalize_recipe(body)


def canonical_recipes() -> dict[Scenario, Recipe]:
    return {scenario: canonical_recipe(scenario) for scenario in _SPECS}


def canonical_hashes() -> dict[str, str]:
    return {scenario.value: recipe.canonical_hash for scenario, recipe in canonical_recipes().items()}


def scene_for_hash(digest: str) -> Scenario | None:
    for scenario, recipe in canonical_recipes().items():
        if recipe.canonical_hash == digest:
            return scenario
    return None
