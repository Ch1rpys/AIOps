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
    # stop：停容器。nginx_upstream / app_http_500：容器保持运行。
    inject: str = "stop"
    # 负例没有注册恢复动作，执行许可必须拒绝，执行次数保持为 0。
    recoverable: bool = True


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
    Scenario.NGINX_UPSTREAM: ScenarioDefinition(
        "Nginx 上游指向错误地址",
        "demo-nginx",
        Action.RESTORE_NGINX_UPSTREAM,
        "NGINX_UPSTREAM_MISCONFIGURED",
        ("nginx_hello",),
        ("demo-nginx", "demo-app"),
        "nginx-upstream",
        "容器保持运行，把代理上游指到无法连接的地址，确认代理返回 502 后再恢复配置。",
        inject="nginx_upstream",
    ),
    Scenario.APP_HTTP_500: ScenarioDefinition(
        "应用固定返回 500",
        "demo-app",
        Action.RESTORE_APP_MODE,
        "APP_HTTP_500",
        ("nginx_hello",),
        ("demo-app", "demo-nginx"),
        "general",
        "容器保持运行，业务接口固定返回 500。闭环可恢复为正常响应；盲诊仍按拒答计分。",
        inject="app_http_500",
        recoverable=False,
    ),
}
