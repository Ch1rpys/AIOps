from typing import Literal

from pydantic import Field, model_validator

from app.config import DomainError, SERVICES
from app.schemas.domain import Scenario, StrictModel

COMPILER_KIND = Literal[
    "life",
    "net",
    "nginx_upstream",
    "nginx_timeout",
    "app_mode",
    "mysql_statement",
    "mysql_password",
    "redis_op",
    "poison_logs",
    "cpu_limit",
    "mem_limit",
    "netem_delay",
    "netem_loss",
    "mysql_max_connections",
    "redis_maxmemory",
    "redis_maxmemory_policy",
]

LIFE_VERBS = ("stop", "pause", "kill", "recreate", "start", "unpause")
NET_OPS = ("disconnect", "connect")
NGINX_PROFILES = ("misdirected", "healthy", "mysql", "blackhole")
APP_MODES = ("http_500", "ok", "slow", "empty", "wrong_json")
CPU_LIMITS = ("0.1", "0.2", "0.5")
MEM_LIMITS = ("32m", "64m", "128m")
NETEM_DELAYS_MS = (50, 100, 200, 500)
NETEM_LOSS_PCT = (5, 10, 20)
MYSQL_MAX_CONNECTIONS = (2, 5, 10)
REDIS_MAXMEMORY = ("1mb", "4mb", "16mb")
REDIS_MAXMEMORY_POLICIES = ("noeviction", "allkeys-lru")


class Primitive(StrictModel):
    kind: COMPILER_KIND
    service: str | None = None
    verb: str | None = None
    op: str | None = None
    profile: str | None = None
    connect: int | None = None
    read: int | None = None
    send: int | None = None
    mode: str | None = None
    statement_id: str | None = None
    cpus: str | None = None
    memory: str | None = None
    delay_ms: int | None = None
    loss_pct: int | None = None
    max_connections: int | None = None
    maxmemory: str | None = None
    policy: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "Primitive":
        if self.service is not None and self.service not in SERVICES:
            raise ValueError("service 不在四个 demo 服务中")
        if self.kind == "life":
            if self.service is None or self.verb not in LIFE_VERBS:
                raise ValueError("life 需要 service 与已登记 verb")
        elif self.kind == "net":
            if self.service is None or self.op not in NET_OPS:
                raise ValueError("net 需要 service 与 connect/disconnect")
        elif self.kind == "nginx_upstream":
            if self.profile not in NGINX_PROFILES:
                raise ValueError("nginx_upstream 需要已登记 profile")
        elif self.kind == "nginx_timeout":
            if not all(isinstance(v, int) and 1 <= v <= 60 for v in (self.connect, self.read, self.send)):
                raise ValueError("nginx_timeout 需要目录网格内的整数")
        elif self.kind == "app_mode":
            if self.mode not in APP_MODES:
                raise ValueError("app_mode 需要已登记模式")
        elif self.kind == "mysql_statement":
            if not self.statement_id:
                raise ValueError("mysql_statement 需要 statement_id")
        elif self.kind == "mysql_password":
            if self.op not in {None, "scramble", "reset"}:
                raise ValueError("mysql_password 只允许 scramble/reset")
        elif self.kind == "redis_op":
            if not self.op:
                raise ValueError("redis_op 需要已登记 op")
        elif self.kind == "cpu_limit":
            if self.service is None or self.cpus not in CPU_LIMITS:
                raise ValueError("cpu_limit 只允许 0.1/0.2/0.5")
        elif self.kind == "mem_limit":
            if self.service is None or self.memory not in MEM_LIMITS:
                raise ValueError("mem_limit 只允许 32m/64m/128m")
        elif self.kind == "netem_delay":
            if self.service is None or self.delay_ms not in NETEM_DELAYS_MS:
                raise ValueError("netem_delay 只允许 50/100/200/500")
        elif self.kind == "netem_loss":
            if self.service is None or self.loss_pct not in NETEM_LOSS_PCT:
                raise ValueError("netem_loss 只允许 5/10/20")
        elif self.kind == "mysql_max_connections":
            if self.max_connections not in MYSQL_MAX_CONNECTIONS:
                raise ValueError("mysql_max_connections 只允许 2/5/10")
        elif self.kind == "redis_maxmemory":
            if self.maxmemory not in REDIS_MAXMEMORY:
                raise ValueError("redis_maxmemory 只允许 1mb/4mb/16mb")
        elif self.kind == "redis_maxmemory_policy":
            if self.policy not in REDIS_MAXMEMORY_POLICIES:
                raise ValueError("redis_maxmemory_policy 只允许 noeviction/allkeys-lru")
        return self


class RecipeBody(StrictModel):
    schema_version: Literal[1] = 1
    id: str = ""
    title: str = Field(min_length=1, max_length=200)
    primitives: list[Primitive] = Field(min_length=1, max_length=4)
    recoverable: bool = True
    negative: bool = False
    poison: bool = False
    canonical_scene: Scenario | None = None
    preferred_actions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bound_primitives(self) -> "RecipeBody":
        if not (1 <= len(self.primitives) <= 4):
            raise ValueError("primitives 必须是 1 到 4 条")
        return self


class RecipeSignature(StrictModel):
    failing_checks: list[str] = Field(default_factory=list)
    passing_checks: list[str] = Field(default_factory=list)
    unknown_checks: list[str] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    calibrated_at: str | None = None
    twin_image_digests: dict[str, str] = Field(default_factory=dict)
    source: Literal["calibrated", "prior"] = "calibrated"


class Recipe(RecipeBody):
    canonical_hash: str = ""
    signature: RecipeSignature | None = None
    holdout: bool = False
    compiler_version: int = 1
    calibrated_at: str | None = None
    dirty: bool = False
    unsupported: bool = False


def parse_recipe(data: dict) -> Recipe:
    payload = dict(data)
    reserved = (
        "canonical_hash",
        "signature",
        "holdout",
        "compiler_version",
        "calibrated_at",
        "dirty",
        "unsupported",
    )
    extras = {key: payload.pop(key) for key in reserved if key in payload}
    try:
        body = RecipeBody.model_validate(payload)
    except Exception as error:
        raise DomainError("RECIPE_INVALID", "配方 DSL 校验失败", 409) from error
    from app.services.compiler.hash import finalize_recipe, identify

    recipe_id, digest = identify(body.primitives)
    given = data.get("id") or body.id
    if given and given != recipe_id:
        raise DomainError("RECIPE_INVALID", "配方 id 与规范哈希不一致", 409)
    extra = {}
    if extras.get("signature"):
        extra["signature"] = RecipeSignature.model_validate(extras["signature"])
    extra["holdout"] = bool(extras.get("holdout"))
    extra["compiler_version"] = int(extras.get("compiler_version") or 1)
    extra["calibrated_at"] = extras.get("calibrated_at")
    extra["dirty"] = bool(extras.get("dirty"))
    extra["unsupported"] = bool(extras.get("unsupported"))
    return finalize_recipe(body, **extra)
