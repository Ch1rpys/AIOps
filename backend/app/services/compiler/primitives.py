"""原语 → 控制器调用。禁止再开一条配方专用 shell。"""

from app.config import CHECK_IDS, DomainError, SERVICES
from app.integrations.docker import (
    APP_MODES,
    CPU_LIMITS,
    DEFAULT_MYSQL_MAX_CONNECTIONS,
    MEM_LIMITS,
    MYSQL_MAX_CONNECTIONS,
    NETEM_DELAYS_MS,
    NETEM_LOSS_PCT,
    REDIS_MAXMEMORY,
    REDIS_MAXMEMORY_POLICIES,
)
from app.services.action_catalog import MYSQL_STATEMENTS, REDIS_OPERATIONS, lookup
from app.services.compiler.schema import Primitive

FAILING_BY_SERVICE = {
    "demo-mysql": ("mysql_select_1", "app_db_check"),
    "demo-redis": ("redis_roundtrip", "app_cache_check"),
    "demo-app": ("nginx_hello", "app_db_check", "app_cache_check", "app_health"),
    "demo-nginx": ("nginx_hello",),
}

# 断开 docker 网络后，宿主机映射端口仍可能通；业务容器互访会失败。
NET_FAILING = {
    "demo-mysql": ("app_db_check",),
    "demo-redis": ("app_cache_check",),
    "demo-app": ("nginx_hello",),
    "demo-nginx": ("nginx_hello",),
}


def prior_failing_checks(primitives: list[Primitive]) -> set[str]:
    """静态效应先验，只用于枚举剪枝。真值以校准为准。"""
    failing: set[str] = set()
    for item in primitives:
        if item.kind == "life" and item.verb in {"stop", "pause", "kill", "recreate"} and item.service:
            failing.update(FAILING_BY_SERVICE.get(item.service, ()))
        elif item.kind == "net" and item.op == "disconnect" and item.service:
            failing.update(NET_FAILING.get(item.service, ()))
        elif item.kind == "nginx_upstream" and item.profile in {"misdirected", "mysql", "blackhole"}:
            failing.add("nginx_hello")
        elif item.kind == "nginx_timeout":
            failing.add("nginx_hello")
        elif item.kind == "app_mode" and item.mode in {"http_500", "slow", "empty", "wrong_json"}:
            failing.update({"nginx_hello", "app_health"} if item.mode != "slow" else {"nginx_hello"})
            if item.mode == "http_500":
                failing.discard("app_health")
        elif item.kind == "mysql_password" and item.op in {None, "scramble"}:
            failing.update({"mysql_select_1", "app_db_check"})
        elif item.kind == "mysql_max_connections":
            failing.update({"mysql_select_1", "app_db_check"})
        elif item.kind in {"redis_maxmemory", "redis_maxmemory_policy"}:
            failing.update({"redis_roundtrip", "app_cache_check"})
        elif item.kind == "cpu_limit" and item.service:
            failing.update(FAILING_BY_SERVICE.get(item.service, ()))
        elif item.kind == "mem_limit" and item.service:
            failing.update(FAILING_BY_SERVICE.get(item.service, ()))
        elif item.kind in {"netem_delay", "netem_loss"} and item.service:
            failing.update(NET_FAILING.get(item.service, ()))
        elif item.kind == "redis_op" and item.op == "flushdb":
            failing.update({"redis_roundtrip", "app_cache_check"})
    return failing


def prior_affected_services(primitives: list[Primitive]) -> list[str]:
    named: list[str] = []
    for item in primitives:
        if item.service and item.service not in named:
            named.append(item.service)
        elif item.kind in {"nginx_upstream", "nginx_timeout"} and "demo-nginx" not in named:
            named.append("demo-nginx")
        elif item.kind == "app_mode" and "demo-app" not in named:
            named.append("demo-app")
        elif item.kind in {"mysql_statement", "mysql_password", "mysql_max_connections"}:
            if "demo-mysql" not in named:
                named.append("demo-mysql")
        elif item.kind in {"redis_op", "redis_maxmemory", "redis_maxmemory_policy"}:
            if "demo-redis" not in named:
                named.append("demo-redis")
    return named


def undo_primitive(item: Primitive) -> Primitive:
    if item.kind == "life":
        undo_verb = {
            "stop": "start",
            "kill": "start",
            "pause": "unpause",
            "recreate": "start",
            "start": "start",
            "unpause": "unpause",
        }[item.verb or "start"]
        return Primitive(kind="life", service=item.service, verb=undo_verb)
    if item.kind == "net":
        return Primitive(kind="net", service=item.service, op="connect" if item.op == "disconnect" else "connect")
    if item.kind == "nginx_upstream":
        return Primitive(kind="nginx_upstream", profile="healthy")
    if item.kind == "nginx_timeout":
        return Primitive(kind="nginx_upstream", profile="healthy")
    if item.kind == "app_mode":
        return Primitive(kind="app_mode", mode="ok")
    if item.kind == "mysql_statement":
        return item
    if item.kind == "mysql_password":
        return Primitive(kind="mysql_password", op="reset")
    if item.kind == "redis_op":
        return item
    if item.kind == "poison_logs":
        return item
    if item.kind == "cpu_limit":
        return Primitive(kind="cpu_limit", service=item.service, cpus="0.1")  # replaced below
    if item.kind == "mem_limit":
        return Primitive(kind="mem_limit", service=item.service, memory="32m")
    if item.kind in {"netem_delay", "netem_loss"}:
        return Primitive(kind="netem_delay", service=item.service, delay_ms=50)
    if item.kind == "mysql_max_connections":
        return Primitive(kind="mysql_max_connections", max_connections=2)
    if item.kind == "redis_maxmemory":
        return Primitive(kind="redis_maxmemory", maxmemory="1mb")
    if item.kind == "redis_maxmemory_policy":
        return Primitive(kind="redis_maxmemory_policy", policy="noeviction")
    raise DomainError("RECIPE_INVALID", "未知原语，无法拆除", 409)


def apply_primitive(docker, item: Primitive, *, undo: bool = False) -> dict:
    """对某个 World 的 docker 控制器执行一条原语。undo 走拆除语义。"""
    if item.kind == "poison_logs":
        return {"operation": "poison_logs", "exit_code": 0}
    if item.kind == "life":
        verb = item.verb or "start"
        if undo:
            verb = {
                "stop": "start",
                "kill": "start",
                "pause": "unpause",
                "recreate": "start",
                "start": "start",
                "unpause": "unpause",
            }.get(verb, "start")
        return docker.change(item.service, verb)
    if item.kind == "net":
        op = item.op or "disconnect"
        if undo:
            op = "connect"
        return docker.network(item.service, op)
    if item.kind == "nginx_upstream":
        profile = "healthy" if undo else (item.profile or "healthy")
        return docker.apply_nginx_upstream(profile)
    if item.kind == "nginx_timeout":
        if undo:
            return docker.apply_nginx_upstream("healthy")
        return docker.apply_nginx_timeouts(item.connect or 2, item.read or 3, item.send or 3)
    if item.kind == "app_mode":
        return docker.apply_app_mode("ok" if undo else (item.mode or "ok"))
    if item.kind == "mysql_statement":
        if undo:
            return {"operation": "mysql", "statement_id": item.statement_id, "exit_code": 0}
        if item.statement_id not in MYSQL_STATEMENTS:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的 SQL", 409)
        return docker.apply_mysql_statement(item.statement_id)
    if item.kind == "mysql_password":
        if undo or item.op == "reset":
            return docker.reset_demo_password()
        return docker.scramble_demo_password()
    if item.kind == "redis_op":
        if undo:
            return {"operation": "redis", "redis_op": item.op, "exit_code": 0}
        if item.op not in REDIS_OPERATIONS:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的 Redis 操作", 409)
        return docker.apply_redis_operation(item.op)
    if item.kind == "cpu_limit":
        cpus = "0" if undo else item.cpus
        if not undo and cpus not in CPU_LIMITS:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的 CPU 限制", 409)
        return docker.update_resources(item.service, cpus=cpus)
    if item.kind == "mem_limit":
        memory = "0" if undo else item.memory
        if not undo and memory not in MEM_LIMITS:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的内存限制", 409)
        return docker.update_resources(item.service, memory=memory)
    if item.kind in {"netem_delay", "netem_loss"}:
        if undo:
            return docker.clear_netem(item.service)
        if item.kind == "netem_delay":
            if item.delay_ms not in NETEM_DELAYS_MS:
                raise DomainError("RECIPE_INVALID", "拒绝未登记的延迟", 409)
            return docker.apply_netem(item.service, delay_ms=item.delay_ms)
        if item.loss_pct not in NETEM_LOSS_PCT:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的丢包率", 409)
        return docker.apply_netem(item.service, loss_pct=item.loss_pct)
    if item.kind == "mysql_max_connections":
        value = DEFAULT_MYSQL_MAX_CONNECTIONS if undo else item.max_connections
        if not undo and value not in MYSQL_MAX_CONNECTIONS:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的 max_connections", 409)
        return docker.set_mysql_max_connections(int(value))
    if item.kind == "redis_maxmemory":
        value = "0" if undo else item.maxmemory
        if not undo and value not in REDIS_MAXMEMORY:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的 maxmemory", 409)
        return docker.set_redis_config("maxmemory", value or "0")
    if item.kind == "redis_maxmemory_policy":
        value = "noeviction" if undo else item.policy
        if not undo and value not in REDIS_MAXMEMORY_POLICIES:
            raise DomainError("RECIPE_INVALID", "拒绝未登记的淘汰策略", 409)
        return docker.set_redis_config("maxmemory-policy", value or "noeviction")
    raise DomainError("RECIPE_INVALID", "未知原语", 409)


def preferred_recovery_actions(primitives: list[Primitive]) -> list[str]:
    """校准前的先验优先恢复动作，R 臂第一名可覆盖。"""
    actions: list[str] = []
    for item in primitives:
        if item.kind == "life" and item.verb in {"stop", "kill", "pause"} and item.service:
            verb = "unpause" if item.verb == "pause" else "start"
            if item.service == "demo-mysql" and verb == "start":
                actions.append("START_DEMO_MYSQL")
            elif item.service == "demo-redis" and verb == "start":
                actions.append("START_DEMO_REDIS")
            elif item.service == "demo-app" and verb == "start":
                actions.append("START_DEMO_APP")
            else:
                actions.append(f"LIFE_{item.service.replace('-', '_').upper()}_{verb.upper()}")
        elif item.kind == "nginx_upstream":
            actions.append("RESTORE_NGINX_UPSTREAM")
        elif item.kind == "nginx_timeout":
            actions.append("RESTORE_NGINX_UPSTREAM")
        elif item.kind == "app_mode":
            actions.append("RESTORE_APP_MODE")
        elif item.kind == "mysql_password":
            actions.append("MYSQL_RESET_DEMO_PASSWORD")
        elif item.kind == "net" and item.op == "disconnect" and item.service:
            actions.append(f"NET_CONNECT_{item.service.replace('-', '_').upper()}")
    unique: list[str] = []
    for action_id in actions:
        if action_id not in unique and lookup(action_id) is not None:
            unique.append(action_id)
    return unique


def assert_grid(item: Primitive) -> None:
    """编译器原语参数只能是网格枚举。"""
    if item.kind == "cpu_limit" and item.cpus not in CPU_LIMITS:
        raise DomainError("RECIPE_INVALID", "CPU 限制不在网格内", 409)
    if item.kind == "mem_limit" and item.memory not in MEM_LIMITS:
        raise DomainError("RECIPE_INVALID", "内存限制不在网格内", 409)
    if item.kind == "app_mode" and item.mode not in APP_MODES:
        raise DomainError("RECIPE_INVALID", "应用模式不在网格内", 409)
    _ = (CHECK_IDS, SERVICES)
