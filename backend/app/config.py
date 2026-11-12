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
    # 视觉模型（通义千问多模态）：模型 ID 以账号实际可用的为准。
    # 空值不伪装成已配置：健康检查会显示 UNKNOWN。
    vision_url: str = ""
    vision_api_key: SecretStr = SecretStr("")
    vision_model: str = ""
    attachment_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    mysql_host: Literal["127.0.0.1"] = "127.0.0.1"
    mysql_port: int = Field(default=13306, ge=1024, le=65535)
    mysql_user: Literal["demo"] = "demo"
    demo_mysql_password: SecretStr = SecretStr("")
    demo_mysql_root_password: SecretStr = SecretStr("")
    app_url: str = "http://127.0.0.1:18000"
    nginx_url: str = "http://127.0.0.1:18080"
    redis_port: int = Field(default=16379, ge=1024, le=65535)
    # 影子世界：独立项目、独立端口、独立配置目录。见计划 §5.1。
    twin_mysql_port: int = Field(default=23306, ge=1024, le=65535)
    twin_redis_port: int = Field(default=26379, ge=1024, le=65535)
    twin_app_url: str = "http://127.0.0.1:28000"
    twin_nginx_url: str = "http://127.0.0.1:28080"
    probe_timeout: int = Field(default=3, ge=1, le=10)
    docker_timeout: int = Field(default=20, ge=1, le=60)
    twin_docker_timeout: int = Field(default=30, ge=1, le=120)
    # 仅开发允许重冻 holdout。演示/生产默认关闭。
    allow_holdout_freeze: bool = False
    # 把 R 臂搜索结果喂给规划模型会污染对照，默认关闭。
    r_arm_planner_context: bool = False
    r_arm_max_candidates: int = Field(default=30, ge=1, le=80)
    model_timeout: int = Field(default=90, ge=1, le=180)
    # 评测专用预算：见 docs/落地执行计划.md §9.4。
    # 采用「评测专用较短超时」方案，使单次调用不可能突破总预算，
    # 「超限终止」才能真正落在两次调用之间而不是让在途调用把预算吃光。
    evaluation_model_timeout: int = Field(default=15, ge=1, le=60)
    evaluation_budget_seconds: int = Field(default=45, ge=5, le=300)
    evaluation_max_model_calls: int = Field(default=3, ge=1, le=10)
    # 未见故障诊断没有六场景提示，模型更慢；单独预算，不挤占 canonical 的 15s×3。
    # 实测 qwen 单次常超过 90s，客户端先超时则签名覆盖永远进不了分母。
    evaluation_unseen_model_timeout: int = Field(default=180, ge=1, le=300)
    evaluation_unseen_budget_seconds: int = Field(default=300, ge=5, le=600)
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
        if self.evaluation_unseen_model_timeout > self.evaluation_unseen_budget_seconds:
            raise ValueError("未见评测超时不能大于未见总预算")
        return self


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(message)
