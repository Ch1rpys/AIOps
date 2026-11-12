import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)


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
    NGINX_UPSTREAM = "NGINX_UPSTREAM"
    APP_HTTP_500 = "APP_HTTP_500"
    DUAL_MYSQL_REDIS = "DUAL_MYSQL_REDIS"


def try_scenario(value: str | None) -> Scenario | None:
    """规范场景返回枚举；编译器工单的 COMPILED 等字符串返回 None。"""
    try:
        return Scenario(value or "MYSQL_DOWN")
    except ValueError:
        return None


class Action(StrEnum):
    START_DEMO_MYSQL = "START_DEMO_MYSQL"
    START_DEMO_REDIS = "START_DEMO_REDIS"
    START_DEMO_APP = "START_DEMO_APP"
    RESTORE_NGINX_UPSTREAM = "RESTORE_NGINX_UPSTREAM"
    RESTORE_APP_MODE = "RESTORE_APP_MODE"
    NO_ACTION = "NO_ACTION"


RootCause = Literal[
    "MYSQL_SERVICE_STOPPED",
    "REDIS_SERVICE_STOPPED",
    "UPSTREAM_APP_STOPPED",
    "NGINX_UPSTREAM_MISCONFIGURED",
    "INSUFFICIENT_EVIDENCE",
    "OTHER",
]


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

    root_cause: RootCause
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
    root_cause: RootCause
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    confidence: float = Field(ge=0, le=1)
    evidence_sufficient: StrictBool
    # M3 扩展：全部可选且有默认值，现有证据与结论序列化结果不变。
    candidates: list[CandidateRootCause] = Field(default_factory=list, max_length=5)
    requested_checks: list[CheckRequest] = Field(default_factory=list, max_length=3)
    # 未见故障：空列表不进旧序列化，避免改口现有断言。
    affected_services: list[str] = Field(default_factory=list, max_length=8)
    failing_check_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_serializer(mode="wrap")
    def _omit_empty_unseen_fields(self, serializer):
        data = serializer(self)
        if not data.get("affected_services"):
            data.pop("affected_services", None)
        if not data.get("failing_check_ids"):
            data.pop("failing_check_ids", None)
        return data


class DomainReview(StrictModel):
    """B 臂的独立复核结论：同意或质疑，并给出自己的根因判断。"""

    verdict: Literal["agree", "disagree"]
    root_cause: RootCause
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    evidence_sufficient: StrictBool


class Plan(StrictModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    action_id: str = Field(
        min_length=1,
        max_length=800,
        validation_alias=AliasChoices(
            "action_id", "actionId", "action", "command", "动作", "动作编号"
        ),
    )
    target: str = Field(
        min_length=1,
        max_length=120,
        validation_alias=AliasChoices("target", "target_service", "目标"),
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        validation_alias=AliasChoices("reason", "rationale", "explanation", "原因"),
    )
    risk: Literal["LOW"] = Field(
        validation_alias=AliasChoices("risk", "risk_level", "风险"),
    )
    expected_checks: list[str] = Field(
        min_length=1,
        max_length=6,
        validation_alias=AliasChoices(
            "expected_checks", "expectedChecks", "checks", "探针", "检查"
        ),
    )

    @field_validator("risk", mode="before")
    @classmethod
    def fold_risk(cls, value: object) -> object:
        if isinstance(value, str):
            folded = re.sub(r"[\s.]+", "", value).lower()
            if folded in {"low", "低", "低风险", "低危", "risklow"}:
                return "LOW"
        return value

    @field_validator("action_id", mode="before")
    @classmethod
    def fold_action_id(cls, value: object) -> object:
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if not isinstance(value, str):
            return value
        from app.services.action_catalog import canonical_action_id

        return canonical_action_id(value)

    @field_validator("target")
    @classmethod
    def fold_target(cls, value: str) -> str:
        from app.services.action_catalog import canonical_target

        return canonical_target(value)

    @field_validator("expected_checks", mode="before")
    @classmethod
    def split_checks(cls, value: object) -> object:
        junk = {"and", "or", "then", "和", "以及"}
        if isinstance(value, str):
            parts = [
                item.strip()
                for item in re.split(r"[,，、;；]+|\band\b|和|以及", value, flags=re.I)
                if item.strip() and item.strip().lower() not in junk
            ]
            return parts or [value]
        if isinstance(value, list):
            return [item for item in value if str(item).strip().lower() not in junk]
        return value

    @field_validator("expected_checks")
    @classmethod
    def fold_checks(cls, value: list[str]) -> list[str]:
        from app.services.action_catalog import canonical_check

        return [canonical_check(item) for item in value]

    @model_validator(mode="after")
    def align_target_with_catalog(self) -> "Plan":
        from app.services.action_catalog import lookup

        entry = lookup(self.action_id)
        if entry is not None:
            self.target = entry.target
        return self


class CompileInjectRequest(StrictModel):
    confirm: StrictBool
    recipe_id: str | None = Field(default=None, max_length=80)
    random: Literal["seen"] | None = None

    @field_validator("confirm")
    @classmethod
    def require_compile_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("操作必须明确确认")
        return value


class Confirmation(StrictModel):
    confirm: StrictBool
    # 工作台可选：把一段错误根因写进日志。只作为证据，不改变允许执行的动作。
    poison: StrictBool = False

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

    scenario: Scenario | None = None
    arms: list[Literal["A", "B", "C", "L"]] = Field(default_factory=lambda: ["A", "B"])
    # adversarial：日志和截图里带投毒文本，单独计分，不进普通正确率分母。
    # unseen：只抽 holdout，按校准签名计分。
    cohort: Literal["standard", "adversarial", "unseen"] = "standard"
    recipe_id: str | None = Field(default=None, max_length=80)
    # C 臂用的截图 id（先 POST /api/evaluation/attachments 上传得到）。
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
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

    @model_validator(mode="after")
    def require_scenario_unless_unseen(self) -> "EvaluationRequest":
        if self.cohort == "unseen":
            return self
        if self.scenario is None:
            raise ValueError("非未见评测必须指定场景")
        return self


class Verification(StrictModel):
    verdict: Verdict
    samples: list[list[Probe]]
    captured_at: str = Field(default_factory=now)
    # M5 连续健康窗口：None = 未启用窗口（默认关闭，行为与现状一致）。
    # 「恢复不稳定」由 window.instability 表达，verdict 仍是 PASS/FAIL/UNKNOWN 三值。
    window: dict | None = None


class IncidentView(StrictModel):
    id: str
    scenario_key: str
    mode: Literal["REAL", "TEST/MOCK"]
    status: Status
    created_at: str
    updated_at: str
    finished_at: str | None
    detail: dict[str, Any]
    records: list[dict[str, Any]]
