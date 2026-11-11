import json
import os
import subprocess

from app.config import COMPOSE_PROJECT, ROOT, SERVICES, DomainError, Settings

NGINX_PROFILES = {
    "healthy": ROOT / "demo/nginx/default.conf",
    "misdirected": ROOT / "demo/nginx/misdirected.conf",
}
NGINX_ACTIVE = ROOT / "demo/nginx/active/default.conf"
APP_MODE_FILE = ROOT / "demo/app-control/mode"
APP_MODES = {"ok", "http_500"}


class DockerController:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = [
            "docker",
            "--context",
            "default",
            "compose",
            "--project-name",
            COMPOSE_PROJECT,
            "--env-file",
            str(ROOT / ".env"),
            "--file",
            str(ROOT / "deploy/docker-compose.yml"),
        ]

    def _run(self, arguments: list[str]) -> str:
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
                timeout=self.settings.docker_timeout,
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

    def change(self, service: str, operation: str) -> dict:
        if service not in SERVICES or operation not in {"start", "stop"}:
            raise DomainError("BLOCKED", "拒绝未注册的资源或操作")
        snapshot = self.snapshot()
        if service not in snapshot or snapshot[service]["project"] != COMPOSE_PROJECT:
            raise DomainError("BLOCKED", "资源不属于固定隔离项目")
        self._run([*self.base, operation, service])
        return {
            "service": service,
            "operation": operation,
            "exit_code": 0,
            "command_template": f"docker compose -p {COMPOSE_PROJECT} {operation} {service}",
        }

    def apply_nginx_upstream(self, profile: str) -> dict:
        """换成仓库里的固定代理配置，再让正在运行的 Nginx 重载。

        profile 只能是 healthy / misdirected。重载参数是固定的 argv，不是拼接出来的命令。
        """
        template = NGINX_PROFILES.get(profile)
        if template is None:
            raise DomainError("BLOCKED", "拒绝未注册的代理配置")
        NGINX_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        NGINX_ACTIVE.write_bytes(template.read_bytes())
        snapshot = {}
        try:
            snapshot = self.snapshot()
        except DomainError:
            snapshot = {}
        if snapshot.get("demo-nginx", {}).get("state") == "running":
            self._run([*self.base, "exec", "-T", "demo-nginx", "nginx", "-s", "reload"])
        return {"profile": profile, "exit_code": 0, "operation": "nginx_upstream"}

    def apply_app_mode(self, mode: str) -> dict:
        """把应用模式写成 ok 或 http_500。应用每次请求自己读这个文件。"""
        if mode not in APP_MODES:
            raise DomainError("BLOCKED", "拒绝未注册的应用模式")
        APP_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_MODE_FILE.write_text(mode + "\n", encoding="utf-8")
        return {"mode": mode, "exit_code": 0, "operation": "app_mode"}

    def logs(self, service: str) -> str:
        if service not in SERVICES:
            raise DomainError("BLOCKED", "日志资源不在白名单")
        return self._run(
            [*self.base, "logs", "--no-color", "--tail", str(self.settings.log_lines), service]
        )[-16000:]
