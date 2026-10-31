import json
import os
import subprocess

from app.config import COMPOSE_PROJECT, ROOT, SERVICES, DomainError, Settings


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

    def logs(self, service: str) -> str:
        if service not in SERVICES:
            raise DomainError("BLOCKED", "日志资源不在白名单")
        return self._run(
            [*self.base, "logs", "--no-color", "--tail", str(self.settings.log_lines), service]
        )[-16000:]
