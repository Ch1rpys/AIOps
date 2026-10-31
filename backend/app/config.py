from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "aiops-demo"
SERVICES = ("demo-mysql", "demo-redis", "demo-app", "demo-nginx")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    admin_token: SecretStr = SecretStr("")
    holmes_url: str = "http://127.0.0.1:5050"
    holmes_api_key: SecretStr = SecretStr("")
    holmes_model: str = ""
    planner_url: str = ""
    planner_api_key: SecretStr = SecretStr("")
    planner_model: str = ""
    holmes_json_mode: Literal["json_schema", "json_object"] = "json_schema"
    planner_json_mode: Literal["json_schema", "json_object"] = "json_schema"
    mysql_host: Literal["127.0.0.1"] = "127.0.0.1"
    mysql_port: int = Field(default=13306, ge=1024, le=65535)
    mysql_user: Literal["demo"] = "demo"
    demo_mysql_password: SecretStr = SecretStr("")
    demo_mysql_root_password: SecretStr = SecretStr("")
    app_url: str = "http://127.0.0.1:18000"
    nginx_url: str = "http://127.0.0.1:18080"
    redis_port: int = Field(default=16379, ge=1024, le=65535)
    probe_timeout: int = Field(default=3, ge=1, le=10)
    docker_timeout: int = Field(default=20, ge=1, le=60)
    model_timeout: int = Field(default=90, ge=1, le=180)
    verification_attempts: int = Field(default=5, ge=1, le=5)
    verification_interval: float = Field(default=3, ge=0, le=5)
    fault_confirmation_attempts: int = Field(default=3, ge=1, le=5)
    fault_confirmation_interval: float = Field(default=2, ge=0, le=5)
    log_lines: int = Field(default=60, ge=1, le=100)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(message)
