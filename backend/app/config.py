from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PROJECT = "aiops-demo"
SERVICES = ("demo-mysql", "demo-redis", "demo-app", "demo-nginx")
# 可补采的检查注册表：模型只能从这里选要补什么（见 docs/落地执行计划.md §6.2）。
CHECK_IDS = (
    "mysql_select_1",
    "app_db_check",
    "redis_roundtrip",
    "app_cache_check",
    "nginx_hello",
    "app_health",
)


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
    # 评测专用预算：见 docs/落地执行计划.md §9.4。
    # 采用「评测专用较短超时」方案，使单次调用不可能突破总预算，
    # 「超限终止」才能真正落在两次调用之间而不是让在途调用把预算吃光。
    evaluation_model_timeout: int = Field(default=15, ge=1, le=60)
    evaluation_budget_seconds: int = Field(default=45, ge=5, le=300)
    evaluation_max_model_calls: int = Field(default=3, ge=1, le=10)
    evaluation_max_extra_collects: int = Field(default=2, ge=0, le=5)
    # M3 受限主动补采：轮次、单轮检查数与总耗时预算都要有上限。
    supplement_max_rounds: int = Field(default=2, ge=0, le=2)
    supplement_max_checks: int = Field(default=2, ge=1, le=3)
    supplement_budget_seconds: int = Field(default=45, ge=5, le=300)
    verification_attempts: int = Field(default=5, ge=1, le=5)
    verification_interval: float = Field(default=3, ge=0, le=5)
    # M5 连续健康窗口：默认关闭，关闭时判定与现状逐字一致。
    verification_window_enabled: bool = False
    verification_window_samples: int = Field(default=5, ge=1, le=20)
    verification_required_passes: int = Field(default=3, ge=1, le=10)
    verification_window_seconds: int = Field(default=30, ge=1, le=300)
    fault_confirmation_attempts: int = Field(default=3, ge=1, le=5)
    fault_confirmation_interval: float = Field(default=2, ge=0, le=5)
    log_lines: int = Field(default=60, ge=1, le=100)

    @model_validator(mode="after")
    def evaluation_budget_covers_all_calls(self) -> "Settings":
        # 单次调用超时 × 允许的调用次数必须落在总预算内，
        # 否则「超限终止」在 httpx 无法中断在途请求的前提下不可实现。
        if self.evaluation_model_timeout * self.evaluation_max_model_calls > (
            self.evaluation_budget_seconds
        ):
            raise ValueError(
                "评测预算不自洽：evaluation_model_timeout × evaluation_max_model_calls "
                "必须不大于 evaluation_budget_seconds"
            )
        return self


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(message)
