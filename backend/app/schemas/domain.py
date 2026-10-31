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


class Verification(StrictModel):
    verdict: Verdict
    samples: list[list[Probe]]
    captured_at: str = Field(default_factory=now)


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
