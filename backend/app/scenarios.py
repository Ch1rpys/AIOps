from dataclasses import dataclass

from app.schemas.domain import Action, Scenario


@dataclass(frozen=True)
class ScenarioDefinition:
    name: str
    target: str
    action: Action
    root_cause: str
    checks: tuple[str, ...]
    logs: tuple[str, ...]
    runbook: str
    description: str


SCENARIOS = {
    Scenario.MYSQL_DOWN: ScenarioDefinition(
        "MySQL 服务停止",
        "demo-mysql",
        Action.START_DEMO_MYSQL,
        "MYSQL_SERVICE_STOPPED",
        ("mysql_select_1", "app_db_check"),
        ("demo-mysql", "demo-app"),
        "mysql",
        "停止隔离数据库，确认 SQL 与业务接口失败后进入诊断。",
    ),
    Scenario.REDIS_DOWN: ScenarioDefinition(
        "Redis 服务停止",
        "demo-redis",
        Action.START_DEMO_REDIS,
        "REDIS_SERVICE_STOPPED",
        ("redis_roundtrip", "app_cache_check"),
        ("demo-redis", "demo-app"),
        "redis",
        "停止隔离缓存，确认 Redis 读写与业务缓存接口失败。",
    ),
    Scenario.NGINX_502: ScenarioDefinition(
        "Nginx 上游不可用",
        "demo-app",
        Action.START_DEMO_APP,
        "UPSTREAM_APP_STOPPED",
        ("nginx_hello",),
        ("demo-nginx", "demo-app"),
        "nginx",
        "停止隔离应用，确认 Nginx 代理真实返回 HTTP 502。",
    ),
}
