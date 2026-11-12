"""组合枚举与约束过滤器。同一代码版本 + 同一原语表 → 同一哈希集合。"""

from itertools import combinations

from app.services.action_catalog import MYSQL_STATEMENTS, REDIS_OPERATIONS
from app.services.compiler.canonical import scene_for_hash
from app.services.compiler.hash import finalize_recipe
from app.services.compiler.primitives import preferred_recovery_actions, prior_failing_checks
from app.services.compiler.schema import (
    APP_MODES,
    CPU_LIMITS,
    MEM_LIMITS,
    MYSQL_MAX_CONNECTIONS,
    NETEM_DELAYS_MS,
    NETEM_LOSS_PCT,
    REDIS_MAXMEMORY,
    REDIS_MAXMEMORY_POLICIES,
    Primitive,
    Recipe,
    RecipeBody,
)
from app.services.compiler.titles import title_from_prior

SERVICES = ("demo-mysql", "demo-redis", "demo-app", "demo-nginx")
TIMEOUT_GRID = ((1, 2, 2), (2, 3, 3), (5, 8, 8))
CONFIG_KINDS = {
    "nginx_upstream",
    "nginx_timeout",
    "app_mode",
    "mysql_password",
    "cpu_limit",
    "mem_limit",
    "netem_delay",
    "netem_loss",
    "mysql_max_connections",
    "redis_maxmemory",
    "redis_maxmemory_policy",
}


def all_single_primitives() -> list[Primitive]:
    items: list[Primitive] = []
    for service in SERVICES:
        for verb in ("stop", "pause", "kill", "recreate"):
            items.append(Primitive(kind="life", service=service, verb=verb))
        items.append(Primitive(kind="net", service=service, op="disconnect"))
        for cpus in CPU_LIMITS:
            items.append(Primitive(kind="cpu_limit", service=service, cpus=cpus))
        for memory in MEM_LIMITS:
            items.append(Primitive(kind="mem_limit", service=service, memory=memory))
        for delay in NETEM_DELAYS_MS:
            items.append(Primitive(kind="netem_delay", service=service, delay_ms=delay))
        for loss in NETEM_LOSS_PCT:
            items.append(Primitive(kind="netem_loss", service=service, loss_pct=loss))
    for profile in ("misdirected", "mysql", "blackhole"):
        items.append(Primitive(kind="nginx_upstream", profile=profile))
    for connect, read, send in TIMEOUT_GRID:
        items.append(Primitive(kind="nginx_timeout", connect=connect, read=read, send=send))
    for mode in APP_MODES:
        if mode != "ok":
            items.append(Primitive(kind="app_mode", mode=mode))
    items.append(Primitive(kind="mysql_password", op="scramble"))
    for statement_id in MYSQL_STATEMENTS:
        items.append(Primitive(kind="mysql_statement", statement_id=statement_id))
    for operation in REDIS_OPERATIONS:
        if operation != "flushall":
            items.append(Primitive(kind="redis_op", op=operation))
    items.append(Primitive(kind="poison_logs"))
    for value in MYSQL_MAX_CONNECTIONS:
        items.append(Primitive(kind="mysql_max_connections", max_connections=value))
    for value in REDIS_MAXMEMORY:
        items.append(Primitive(kind="redis_maxmemory", maxmemory=value))
    for value in REDIS_MAXMEMORY_POLICIES:
        items.append(Primitive(kind="redis_maxmemory_policy", policy=value))
    return items


def _life_of(item: Primitive) -> tuple[str, str] | None:
    if item.kind == "life" and item.service and item.verb:
        return item.service, item.verb
    return None


def legal(primitives: list[Primitive]) -> bool:
    if not (1 <= len(primitives) <= 4):
        return False
    stopped: set[str] = set()
    life_by_service: dict[str, set[str]] = {}
    nginx_config = 0
    app_modes = 0
    poisons = 0
    for item in primitives:
        life = _life_of(item)
        if life:
            service, verb = life
            life_by_service.setdefault(service, set()).add(verb)
            if verb in {"stop", "pause", "kill"}:
                stopped.add(service)
        if item.kind in {"nginx_upstream", "nginx_timeout"}:
            nginx_config += 1
        if item.kind == "app_mode":
            app_modes += 1
        if item.kind == "poison_logs":
            poisons += 1
    if len(stopped) >= 4:
        return False
    for verbs in life_by_service.values():
        if len(verbs) > 1:
            return False
        if "stop" in verbs and "pause" in verbs:
            return False
    if nginx_config > 1 or app_modes > 1 or poisons > 1:
        return False
    return True


def annotate(primitives: list[Primitive]) -> Recipe:
    failing = prior_failing_checks(primitives)
    poison = any(item.kind == "poison_logs" for item in primitives)
    if poison and len(primitives) == 1:
        negative = True
    elif not failing:
        negative = True
    else:
        negative = False
    if poison and not failing:
        negative = True
    scene = scene_for_hash(
        __import__("app.services.compiler.hash", fromlist=["canonical_hash"]).canonical_hash(
            primitives
        )
    )
    body = RecipeBody(
        title=title_from_prior(primitives),
        primitives=primitives,
        recoverable=True,
        negative=negative,
        poison=poison,
        canonical_scene=scene,
        preferred_actions=preferred_recovery_actions(primitives),
    )
    return finalize_recipe(body)


def _is_stop(item: Primitive) -> bool:
    return item.kind == "life" and item.verb in {"stop", "kill"}


def _is_config(item: Primitive) -> bool:
    return item.kind in CONFIG_KINDS


def enumerate_recipes() -> list[Recipe]:
    singles = all_single_primitives()
    seen: dict[str, Recipe] = {}

    def add(primitives: list[Primitive]) -> None:
        if not legal(primitives):
            return
        recipe = annotate(primitives)
        seen.setdefault(recipe.canonical_hash, recipe)

    for item in singles:
        add([item])
    for left, right in combinations(singles, 2):
        add([left, right])

    # 3/4 原语只保留「双停 + 配置」和「双停 + 断网 + 配置」白名单，避免组合爆炸。
    stop_only = [
        item for item in singles if item.kind == "life" and item.verb == "stop" and item.service
    ]
    small_configs = [
        Primitive(kind="nginx_upstream", profile="misdirected"),
        Primitive(kind="nginx_timeout", connect=1, read=2, send=2),
        Primitive(kind="app_mode", mode="http_500"),
        Primitive(kind="mysql_password", op="scramble"),
        Primitive(kind="nginx_upstream", profile="blackhole"),
        Primitive(kind="app_mode", mode="wrong_json"),
    ]
    leftover_net = [
        item for item in singles if item.kind == "net" and item.service in {"demo-app", "demo-nginx"}
    ]
    for a, b in combinations(stop_only, 2):
        for config in small_configs:
            add([a, b, config])
        for net in leftover_net:
            add([a, b, net])
            for config in small_configs:
                add([a, b, net, config])
    return sorted(seen.values(), key=lambda recipe: recipe.canonical_hash)


def eligible_pool(recipes: list[Recipe] | None = None) -> list[Recipe]:
    """已有先验失败探针、非 canonical、非 negative，用作冻结候选。"""
    recipes = recipes if recipes is not None else enumerate_recipes()
    return [
        recipe
        for recipe in recipes
        if not recipe.negative
        and recipe.canonical_scene is None
        and prior_failing_checks(recipe.primitives)
    ]
