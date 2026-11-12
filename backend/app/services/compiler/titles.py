"""人类可读层：只描述可观测症状，不写枚举根因名。"""

from app.services.compiler.primitives import prior_affected_services, prior_failing_checks
from app.services.compiler.schema import Primitive, RecipeSignature

PROBE_PHRASES = {
    "mysql_select_1": "数据库连通失败",
    "app_db_check": "业务库检查失败",
    "redis_roundtrip": "缓存读写失败",
    "app_cache_check": "业务缓存检查失败",
    "nginx_hello": "代理入口失败",
    "app_health": "应用健康检查失败",
}


def title_from_checks(failing: list[str] | set[str]) -> str:
    phrases = [PROBE_PHRASES[name] for name in failing if name in PROBE_PHRASES]
    if not phrases:
        return "探针仍全部通过的扰动"
    # 去重但保序
    unique: list[str] = []
    for phrase in phrases:
        if phrase not in unique:
            unique.append(phrase)
    return "且".join(unique)


def title_from_prior(primitives: list[Primitive]) -> str:
    return title_from_checks(prior_failing_checks(primitives))


def confirmation_text(primitives: list[Primitive], signature: RecipeSignature | None = None) -> str:
    failing = (
        list(signature.failing_checks)
        if signature and signature.failing_checks
        else sorted(prior_failing_checks(primitives))
    )
    services = (
        list(signature.affected_services)
        if signature and signature.affected_services
        else prior_affected_services(primitives)
    )
    title = title_from_checks(failing)
    touched = "、".join(services) if services else "隔离服务"
    return f"这是设计好的组合故障。将改动 {touched}。预期可见症状：{title}。"
