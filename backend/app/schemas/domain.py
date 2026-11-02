from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scenario(StrEnum):
    MYSQL_DOWN = "MYSQL_DOWN"
    REDIS_DOWN = "REDIS_DOWN"
    NGINX_502 = "NGINX_502"


class Action(StrEnum):
    START_DEMO_MYSQL = "START_DEMO_MYSQL"
    START_DEMO_REDIS = "START_DEMO_REDIS"
    START_DEMO_APP = "START_DEMO_APP"
    NO_ACTION = "NO_ACTION"


class Status(StrEnum):
    CREATED = "CREATED"
    INJECTED = "INJECTED"
    COLLECTING = "COLLECTING"
    DIAGNOSING = "DIAGNOSING"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"


TERMINAL = {
    Status.RESOLVED,
    Status.CANCELLED,
    Status.FAILED,
    Status.UNKNOWN,
    Status.ERROR,
    Status.BLOCKED,
}


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Probe(StrictModel):
    name: str
    verdict: Verdict
    observed: dict[str, Any]
    captured_at: str = Field(default_factory=now)


class Evidence(StrictModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    source: str
    kind: str
    raw: dict[str, Any]
    captured_at: str = Field(default_factory=now)
    # M2 视觉附件元信息：可选字段，现有证据不填时序列化结果不变。
    # 放在 Evidence 侧而不是 Diagnosis / Plan：给模型看的契约要保持精简。
    attachment: dict[str, Any] | None = None


class VisionAnomaly(StrictModel):
    """截图里识别出的异常区域。"""

    description: str = Field(min_length=1, max_length=500)
    region: list[float] = Field(default_factory=list, max_length=4)
    text: str = Field(default="", max_length=500)


class VisionObservation(StrictModel):
    """截图里能读出来的观测。

    observed_at 是「图中可识别的观测时间」：截图里没有时间戳就必须为 null，
    不允许拿上传时间顶替。
    """

    observed_at: str | None = None
    extracted_text: str = Field(default="", max_length=4000)
    anomalies: list[VisionAnomaly] = Field(default_factory=list, max_length=10)
    uncertain: list[str] = Field(default_factory=list, max_length=10)


REQUIRED_EVIDENCE_KINDS = frozenset({"container_state", "probe"})


def required_evidence_ids(evidence: list[Evidence]) -> list[str]:
    """必须被引用的证据 id：容器状态与全部探针。

    引用完整性校验与盲诊提示词共用这一份判据，
    避免「提示词只说相关、校验要求全部」这种措辞差异被记成模型能力不足。
    """
    return [item.id for item in evidence if item.kind in REQUIRED_EVIDENCE_KINDS]


class CandidateRootCause(StrictModel):
    """候选根因及其支持与反对证据。

    「反对证据」是 M3 的价值所在：新增证据排除了什么，要能核对。
    """

    root_cause: Literal[
        "MYSQL_SERVICE_STOPPED",
        "REDIS_SERVICE_STOPPED",
        "UPSTREAM_APP_STOPPED",
        "INSUFFICIENT_EVIDENCE",
        "OTHER",
    ]
    support_evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    against_evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    missing_checks: list[str] = Field(default_factory=list, max_length=10)


class CheckRequest(StrictModel):
    """模型请求补采的检查：只能从注册的检查 ID 里选，并说明理由。"""

    check_id: Literal[
        "mysql_select_1",
        "app_db_check",
        "redis_roundtrip",
        "app_cache_check",
        "nginx_hello",
        "app_health",
    ]
    reason: str = Field(min_length=1, max_length=500)


class Diagnosis(StrictModel):
    root_cause: Literal[
        "MYSQL_SERVICE_STOPPED",
        "REDIS_SERVICE_STOPPED",
        "UPSTREAM_APP_STOPPED",
        "INSUFFICIENT_EVIDENCE",
        "OTHER",
    ]
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    confidence: float = Field(ge=0, le=1)
    evidence_sufficient: StrictBool
    # M3 扩展：全部可选且有默认值，现有证据与结论序列化结果不变。
    candidates: list[CandidateRootCause] = Field(default_factory=list, max_length=5)
    requested_checks: list[CheckRequest] = Field(default_factory=list, max_length=3)


class DomainReview(StrictModel):
    """B 臂的独立复核结论：同意或质疑，并给出自己的根因判断。"""

    verdict: Literal["agree", "disagree"]
    root_cause: Literal[
        "MYSQL_SERVICE_STOPPED",
        "REDIS_SERVICE_STOPPED",
        "UPSTREAM_APP_STOPPED",
        "INSUFFICIENT_EVIDENCE",
        "OTHER",
    ]
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    evidence_sufficient: StrictBool


class Plan(StrictModel):
    action_id: Action
    target: Literal["demo-mysql", "demo-redis", "demo-app", "none"]
    reason: str = Field(min_length=1, max_length=2000)
    risk: Literal["LOW"]
    expected_checks: list[
        Literal[
            "mysql_select_1", "app_db_check", "redis_roundtrip", "app_cache_check", "nginx_hello"
        ]
    ]


class Confirmation(StrictModel):
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("操作必须明确确认")
        return value


class ApprovalRequest(StrictModel):
    plan_id: str = Field(min_length=1, max_length=80)
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)


class AttachmentRequest(StrictModel):
    """上传一张真实错误页面或运维截图。"""

    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(min_length=1, max_length=20_000_000)
    note: str = Field(default="", max_length=500)


class EvaluationRequest(StrictModel):
    """触发一次盲诊实验。scenario 是注入指令，由管理员选择，不进模型上下文。"""

    scenario: Scenario
    arms: list[Literal["A", "B", "C"]] = Field(default_factory=lambda: ["A", "B"])
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("评测必须明确确认")
        return value

    @field_validator("arms")
    @classmethod
    def require_known_arms(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("至少选择一个实验臂")
        if len(set(value)) != len(value):
            raise ValueError("实验臂不可重复")
        return value


class Verification(StrictModel):
    verdict: Verdict
    samples: list[list[Probe]]
    captured_at: str = Field(default_factory=now)
    # M5 连续健康窗口：None = 未启用窗口（默认关闭，行为与现状一致）。
    # 「恢复不稳定」由 window.instability 表达，verdict 仍是 PASS/FAIL/UNKNOWN 三值。
    window: dict | None = None


class IncidentView(StrictModel):
    id: str
    scenario_key: Scenario
    mode: Literal["REAL", "TEST/MOCK"]
    status: Status
    created_at: str
    updated_at: str
    finished_at: str | None
    detail: dict[str, Any]
    records: list[dict[str, Any]]
