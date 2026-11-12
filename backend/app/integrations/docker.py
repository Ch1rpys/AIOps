import json
import os
import subprocess

from app.config import ROOT, SERVICES, DomainError, Settings
from app.worlds import World, world_from_settings

NGINX_PROFILES = {
    "healthy": ROOT / "demo/nginx/default.conf",
    "misdirected": ROOT / "demo/nginx/misdirected.conf",
    "mysql": ROOT / "demo/nginx/mysql-upstream.conf",
    "blackhole": ROOT / "demo/nginx/blackhole.conf",
}
APP_MODES = {"ok", "http_500", "slow", "empty", "wrong_json"}
CPU_LIMITS = ("0.1", "0.2", "0.5")
MEM_LIMITS = ("32m", "64m", "128m")
NETEM_DELAYS_MS = (50, 100, 200, 500)
NETEM_LOSS_PCT = (5, 10, 20)
MYSQL_MAX_CONNECTIONS = (2, 5, 10)
REDIS_MAXMEMORY = ("1mb", "4mb", "16mb")
REDIS_MAXMEMORY_POLICIES = ("noeviction", "allkeys-lru")
# 仓库内第二份已知乱码：打乱实验密码只能用这一条，禁止模型提供字符串。
SCRAMBLED_MYSQL_PASSWORD = "AiopsScrambled!9f3a2c"
DEFAULT_MYSQL_MAX_CONNECTIONS = 151


class DockerController:
    def __init__(self, settings: Settings, world: World | None = None):
        self.settings = settings
        self.world = world or world_from_settings(settings, "primary")
        self.timeout = (
            settings.twin_docker_timeout if self.world.name == "twin" else settings.docker_timeout
        )
        self.base = [
            "docker",
            "--context",
            "default",
            "compose",
            "--project-name",
            self.world.project,
            "--env-file",
            str(ROOT / ".env"),
            "--file",
            str(self.world.compose_file),
        ]

    def _run(self, arguments: list[str], timeout: int | None = None) -> str:
        # 忽略主机上可能指向远程 Docker 的变量，只使用默认本地引擎。
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
        }
        try:
            result = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                check=False,
                shell=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DomainError("DOCKER_UNAVAILABLE", "Docker 不可用或调用超时", 503) from error
        if result.returncode != 0:
            # CLI 错误可能回显配置值，只向调用者返回稳定错误码。
            raise DomainError(
                "DOCKER_COMMAND_FAILED",
                "Docker 调用失败，请检查隔离项目与引擎",
                503,
                {"exit_code": result.returncode},
            )
        return result.stdout

    def snapshot(self) -> dict:
        text = self._run([*self.base, "ps", "--all", "--format", "json"])
        try:
            rows = (
                json.loads(text)
                if text.strip().startswith("[")
                else [json.loads(line) for line in text.splitlines() if line.strip()]
            )
            return {
                row["Service"]: {
                    "state": row.get("State"),
                    "health": row.get("Health", ""),
                    "project": row.get("Project"),
                }
                for row in rows
                if row.get("Service") in SERVICES
            }
        except (ValueError, TypeError, KeyError) as error:
            raise DomainError("DOCKER_RESPONSE_INVALID", "Docker 状态格式异常", 503) from error

    def _container(self, service: str) -> str:
        container = self._run([*self.base, "ps", "-aq", service]).strip().splitlines()
        if not container:
            raise DomainError("BLOCKED", "隔离容器不存在")
        return container[0]

    def change(self, service: str, operation: str) -> dict:
        lifecycle = {"start", "stop", "restart", "pause", "unpause", "kill"}
        if service not in SERVICES or operation not in {*lifecycle, "recreate"}:
            raise DomainError("BLOCKED", "拒绝未注册的资源或操作")
        snapshot = self.snapshot()
        if service not in snapshot or snapshot[service]["project"] != self.world.project:
            raise DomainError("BLOCKED", "资源不属于固定隔离项目")
        if operation == "recreate":
            self._run([*self.base, "up", "-d", "--force-recreate", "--no-deps", service])
        else:
            self._run([*self.base, operation, service])
        return {
            "service": service,
            "operation": operation,
            "exit_code": 0,
            "command_template": f"docker compose -p {self.world.project} {operation} {service}",
        }

    def apply_nginx_timeouts(self, connect: int, read: int, send: int) -> dict:
        """仍只代理 demo-app。超时只能是目录里的整数，不能是模型拼出来的配置。"""
        if not all(isinstance(value, int) and 1 <= value <= 60 for value in (connect, read, send)):
            raise DomainError("BLOCKED", "拒绝未注册的代理超时")
        self.world.nginx_active.parent.mkdir(parents=True, exist_ok=True)
        self.world.nginx_active.write_text(
            "\n".join(
                [
                    "server {",
                    "    listen 80;",
                    "    resolver 127.0.0.11 valid=5s ipv6=off;",
                    "    location / {",
                    "        set $upstream demo-app:8000;",
                    "        proxy_pass http://$upstream;",
                    f"        proxy_connect_timeout {connect}s;",
                    f"        proxy_read_timeout {read}s;",
                    f"        proxy_send_timeout {send}s;",
                    "    }",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._reload_nginx()
        return {
            "operation": "nginx_timeout",
            "connect_timeout": connect,
            "read_timeout": read,
            "send_timeout": send,
            "exit_code": 0,
        }

    def apply_mysql_statement(self, statement_id: str) -> dict:
        from app.services.action_catalog import MYSQL_STATEMENTS

        statement = MYSQL_STATEMENTS.get(statement_id)
        if statement is None:
            raise DomainError("BLOCKED", "拒绝未登记的 SQL")
        self._mysql(statement)
        return {"operation": "mysql", "statement_id": statement_id, "exit_code": 0}

    def reset_demo_password(self) -> dict:
        password = self.settings.demo_mysql_password.get_secret_value().replace("'", "")
        if not password:
            raise DomainError("BLOCKED", "未配置实验数据库密码")
        self._mysql(f"ALTER USER 'demo'@'%' IDENTIFIED BY '{password}'")
        self._mysql("FLUSH PRIVILEGES")
        return {"operation": "mysql_password", "exit_code": 0}

    def scramble_demo_password(self) -> dict:
        """把实验账号改成仓库里第二份已知乱码。禁止接受外部密码字符串。"""
        password = SCRAMBLED_MYSQL_PASSWORD.replace("'", "")
        self._mysql(f"ALTER USER 'demo'@'%' IDENTIFIED BY '{password}'")
        self._mysql("FLUSH PRIVILEGES")
        return {"operation": "mysql_password_scramble", "exit_code": 0}

    def set_mysql_max_connections(self, value: int) -> dict:
        if value not in {*MYSQL_MAX_CONNECTIONS, DEFAULT_MYSQL_MAX_CONNECTIONS}:
            raise DomainError("BLOCKED", "拒绝未登记的 max_connections")
        self._mysql(f"SET GLOBAL max_connections={int(value)}")
        return {"operation": "mysql_max_connections", "max_connections": value, "exit_code": 0}

    def _mysql(self, statement: str) -> None:
        import pymysql

        with pymysql.connect(
            host=self.settings.mysql_host,
            port=self.world.mysql_port,
            user="root",
            password=self.settings.demo_mysql_root_password.get_secret_value(),
            database="demo",
            connect_timeout=self.settings.probe_timeout,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement)
            connection.commit()

    def apply_redis_operation(self, operation: str) -> dict:
        import redis

        if operation not in {
            "ping",
            "dbsize",
            "info",
            "get_health",
            "set_health",
            "del_health",
            "flushdb",
        }:
            raise DomainError("BLOCKED", "拒绝未登记的 Redis 操作")
        with redis.Redis(
            host="127.0.0.1",
            port=self.world.redis_port,
            socket_connect_timeout=self.settings.probe_timeout,
            socket_timeout=self.settings.probe_timeout,
        ) as connection:
            if operation == "ping":
                connection.ping()
            elif operation == "dbsize":
                connection.dbsize()
            elif operation == "info":
                connection.info("server")
            elif operation == "get_health":
                connection.get("aiops:health")
            elif operation == "set_health":
                connection.set("aiops:health", "probe", ex=30)
            elif operation == "del_health":
                connection.delete("aiops:health")
            elif operation == "flushdb":
                connection.flushdb()
        return {"operation": "redis", "redis_op": operation, "exit_code": 0}

    def set_redis_config(self, key: str, value: str) -> dict:
        import redis

        allowed = {
            ("maxmemory", "0"),
            *[("maxmemory", item) for item in REDIS_MAXMEMORY],
            ("maxmemory-policy", "noeviction"),
            ("maxmemory-policy", "allkeys-lru"),
            ("maxmemory-policy", "volatile-lru"),
        }
        if (key, value) not in allowed:
            raise DomainError("BLOCKED", "拒绝未登记的 Redis 配置")
        with redis.Redis(
            host="127.0.0.1",
            port=self.world.redis_port,
            socket_connect_timeout=self.settings.probe_timeout,
            socket_timeout=self.settings.probe_timeout,
        ) as connection:
            connection.config_set(key, value)
        return {"operation": "redis_config", "key": key, "value": value, "exit_code": 0}

    def network(self, service: str, operation: str) -> dict:
        if service not in SERVICES or operation not in {"connect", "disconnect"}:
            raise DomainError("BLOCKED", "拒绝未登记的网络操作")
        container = self._container(service)
        if operation == "disconnect":
            self._run(
                [
                    "docker",
                    "--context",
                    "default",
                    "network",
                    "disconnect",
                    self.world.network,
                    container,
                ]
            )
        else:
            # 先断开再带 alias 连回。只 connect 不会补上 Compose 服务名，Docker DNS 会落到宿主机假 IP。
            try:
                self._run(
                    [
                        "docker",
                        "--context",
                        "default",
                        "network",
                        "disconnect",
                        self.world.network,
                        container,
                    ]
                )
            except DomainError:
                pass
            self._run(
                [
                    "docker",
                    "--context",
                    "default",
                    "network",
                    "connect",
                    "--alias",
                    service,
                    self.world.network,
                    container,
                ]
            )
        return {"operation": "net", "net_op": operation, "service": service, "exit_code": 0}

    def apply_nginx_upstream(self, profile: str) -> dict:
        """换成仓库里的固定代理配置，再让正在运行的 Nginx 重载。"""
        template = NGINX_PROFILES.get(profile)
        if template is None:
            raise DomainError("BLOCKED", "拒绝未注册的代理配置")
        self.world.nginx_active.parent.mkdir(parents=True, exist_ok=True)
        self.world.nginx_active.write_bytes(template.read_bytes())
        self._reload_nginx()
        return {"profile": profile, "exit_code": 0, "operation": "nginx_upstream"}

    def _reload_nginx(self) -> None:
        snapshot = {}
        try:
            snapshot = self.snapshot()
        except DomainError:
            snapshot = {}
        try:
            self._run([*self.base, "exec", "-T", "demo-nginx", "nginx", "-s", "reload"])
        except DomainError:
            # nginx 未运行时无法重载；活动配置已经写到 bind mount，下次 start 会读到。
            pass

    def apply_app_mode(self, mode: str) -> dict:
        """把应用模式写成已登记值。应用每次请求自己读这个文件。"""
        if mode not in APP_MODES:
            raise DomainError("BLOCKED", "拒绝未注册的应用模式")
        self.world.app_mode_file.parent.mkdir(parents=True, exist_ok=True)
        self.world.app_mode_file.write_text(mode + "\n", encoding="utf-8")
        return {"mode": mode, "exit_code": 0, "operation": "app_mode"}

    def update_resources(self, service: str, cpus: str | None = None, memory: str | None = None) -> dict:
        if service not in SERVICES:
            raise DomainError("BLOCKED", "拒绝未注册的资源或操作")
        arguments = ["docker", "--context", "default", "update"]
        if cpus is not None:
            if cpus not in {*CPU_LIMITS, "0"}:
                raise DomainError("BLOCKED", "拒绝未登记的 CPU 限制")
            arguments.extend(["--cpus", cpus])
        if memory is not None:
            if memory not in {*MEM_LIMITS, "0"}:
                raise DomainError("BLOCKED", "拒绝未登记的内存限制")
            if memory == "0":
                arguments.extend(["--memory", "0", "--memory-swap", "-1"])
            else:
                arguments.extend(["--memory", memory, "--memory-swap", memory])
        if len(arguments) == 4:
            raise DomainError("BLOCKED", "拒绝空的资源更新")
        arguments.append(self._container(service))
        self._run(arguments)
        return {
            "operation": "resources",
            "service": service,
            "cpus": cpus,
            "memory": memory,
            "exit_code": 0,
        }

    def apply_netem(self, service: str, delay_ms: int | None = None, loss_pct: int | None = None) -> dict:
        if service not in SERVICES:
            raise DomainError("BLOCKED", "拒绝未注册的资源或操作")
        if delay_ms is not None and delay_ms not in NETEM_DELAYS_MS:
            raise DomainError("BLOCKED", "拒绝未登记的延迟")
        if loss_pct is not None and loss_pct not in NETEM_LOSS_PCT:
            raise DomainError("BLOCKED", "拒绝未登记的丢包率")
        parts = ["tc", "qdisc", "replace", "dev", "eth0", "root", "netem"]
        if delay_ms is not None:
            parts.extend(["delay", f"{delay_ms}ms"])
        if loss_pct is not None:
            parts.extend(["loss", f"{loss_pct}%"])
        if parts[-1] == "netem":
            raise DomainError("BLOCKED", "拒绝空的 netem")
        try:
            self._run([*self.base, "exec", "-T", "-u", "root", service, *parts])
        except DomainError as error:
            raise DomainError(
                "PRIMITIVE_UNSUPPORTED",
                "当前环境无法设置 netem（需要 NET_ADMIN）",
                409,
            ) from error
        return {
            "operation": "netem",
            "service": service,
            "delay_ms": delay_ms,
            "loss_pct": loss_pct,
            "exit_code": 0,
        }

    def clear_netem(self, service: str) -> dict:
        if service not in SERVICES:
            raise DomainError("BLOCKED", "拒绝未注册的资源或操作")
        try:
            self._run(
                [
                    *self.base,
                    "exec",
                    "-T",
                    "-u",
                    "root",
                    service,
                    "tc",
                    "qdisc",
                    "del",
                    "dev",
                    "eth0",
                    "root",
                ]
            )
        except DomainError:
            # 没有 qdisc 时删除失败，视为已经干净。
            pass
        return {"operation": "netem_clear", "service": service, "exit_code": 0}

    def compose_up(self, services: tuple[str, ...] = SERVICES) -> dict:
        self._run(
            [*self.base, "up", "-d", "--wait", "--wait-timeout", "180", *services],
            timeout=max(self.timeout, 180),
        )
        return {"operation": "compose_up", "project": self.world.project, "exit_code": 0}

    def compose_down(self, volumes: bool = False) -> dict:
        arguments = [*self.base, "down"]
        if volumes:
            arguments.append("-v")
        self._run(arguments, timeout=max(self.timeout, 120))
        return {"operation": "compose_down", "project": self.world.project, "exit_code": 0}

    def image_digests(self) -> dict[str, str]:
        digests: dict[str, str] = {}
        for service in SERVICES:
            try:
                container = self._container(service)
            except DomainError:
                continue
            image = self._run(
                [
                    "docker",
                    "--context",
                    "default",
                    "inspect",
                    "--format",
                    "{{.Image}}",
                    container,
                ]
            ).strip()
            if image:
                digests[service] = image
        return digests

    def logs(self, service: str) -> str:
        if service not in SERVICES:
            raise DomainError("BLOCKED", "日志资源不在白名单")
        return self._run(
            [*self.base, "logs", "--no-color", "--tail", str(self.settings.log_lines), service]
        )[-16000:]
