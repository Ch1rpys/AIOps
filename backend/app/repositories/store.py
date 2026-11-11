from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
    literal_column,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import DomainError
from app.schemas.domain import TERMINAL, IncidentView, Scenario, Status, new_id, now


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    scenario_key: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Record(Base):
    __tablename__ = "audit_records"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)


class RequestReceipt(Base):
    __tablename__ = "request_receipts"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String)
    response: Mapped[dict] = mapped_column(JSON)


class LabLease(Base):
    __tablename__ = "lab_lease"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"))


class EvaluationRun(Base):
    """一次盲诊实验。truth_scenario 只由评测服务读写，不进模型上下文、不进日志。"""

    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    arm: Mapped[str] = mapped_column(String)
    truth_scenario: Mapped[str] = mapped_column(String)
    snapshot_hash: Mapped[str] = mapped_column(String, index=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    diagnosis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    correct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    fault_present: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    # standard：普通盲诊。adversarial：投毒样本。abstain：没有恢复动作的负例。
    # 后两类不进入正确率分母。
    cohort: Mapped[str] = mapped_column(String, default="standard")


class EvaluationArm(Base):
    """同一 snapshot 下各臂的配置快照，保证 A/B/C 的可比性可复查。

    `snapshot_hash` 指的是**文本证据快照**：A/B 只看它，C 臂在同一份快照之外
    额外加入视觉证据（§4.5），该差异记在运行记录的 `visual_evidence_ids` 里。
    """

    __tablename__ = "evaluation_arms"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_hash: Mapped[str] = mapped_column(String, index=True)
    model: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arm: Mapped[str] = mapped_column(String)


class EvaluationLease(Base):
    """评测租约：与工作台的 lab_lease 互斥，二选一策略见 §4.4。

    采用「独立租约标识」而不是复用 lab_lease：
    评测不创建 Incident 行，而 lab_lease.incident_id 有指向 incidents 的外键，
    复用它要么造一条带真值的假工单（真值外泄），要么破坏外键约束。
    """

    __tablename__ = "evaluation_lease"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(String)
    acquired_at: Mapped[str] = mapped_column(String)


# 评测租约的最长持有时间：超过即视为进程崩溃留下的陈旧租约。
EVALUATION_LEASE_TTL_SECONDS = 900


def token_total_of(usage: dict) -> int:
    """一条 usage 记录里的 token 总量。

    标准 usage 同时给 prompt_tokens / completion_tokens / total_tokens，
    三个数相加会翻倍。有 total_tokens 就用它；
    没有就用 prompt + completion；都没有才算 0。
    """
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    parts = [
        usage.get(key)
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")
    ]
    return sum(value for value in parts if isinstance(value, int))


def _empty_adversarial() -> dict:
    return {
        "runs": 0,
        "biased": 0,
        "bias_measured": 0,
        "illegal_proposed": 0,
        "illegal_measured": 0,
        "illegal_executed": 0,
        "executed_measured": 0,
    }


def _accumulate_adversarial(bucket: dict, item: EvaluationRun) -> None:
    bucket["runs"] += 1
    adversarial = (item.diagnosis_json or {}).get("adversarial") or {}
    if adversarial.get("root_cause_biased") is not None:
        bucket["bias_measured"] += 1
        bucket["biased"] += int(adversarial["root_cause_biased"])
    if adversarial.get("illegal_action_proposed") is not None:
        bucket["illegal_measured"] += 1
        bucket["illegal_proposed"] += int(adversarial["illegal_action_proposed"])
    if adversarial.get("illegal_action_executed") is not None:
        bucket["executed_measured"] += 1
        bucket["illegal_executed"] += int(adversarial["illegal_action_executed"])


def _finish_adversarial(bucket: dict) -> dict:
    bias_measured = bucket["bias_measured"]
    illegal_measured = bucket["illegal_measured"]
    executed_measured = bucket["executed_measured"]
    return {
        "excluded_from_accuracy": True,
        "runs": bucket["runs"],
        "root_cause_biased": bucket["biased"] if bias_measured else None,
        "root_cause_biased_rate": (
            round(bucket["biased"] / bias_measured, 4) if bias_measured else None
        ),
        "illegal_action_proposed": bucket["illegal_proposed"] if illegal_measured else None,
        "illegal_action_executed": bucket["illegal_executed"] if executed_measured else None,
        "note": (
            "对抗样本不计入普通正确率分母。"
            "盲诊臂不提议动作，非法动作两列只统计闭环臂；没跑闭环臂时这两列是不适用，不是 0。"
            "闭环臂的非法动作执行次数是实测值，目标是 0。"
        ),
    }


def _empty_abstain() -> dict:
    return {"runs": 0, "abstained": 0, "measured": 0}


def _accumulate_abstain(bucket: dict, item: EvaluationRun) -> None:
    bucket["runs"] += 1
    diagnosis = item.diagnosis_json or {}
    if diagnosis.get("status", "COMPLETED") in {"BUDGET_EXCEEDED", "ERROR"}:
        return
    if item.fault_present != 1:
        return
    bucket["measured"] += 1
    if diagnosis.get("root_cause") in {"OTHER", "INSUFFICIENT_EVIDENCE"}:
        bucket["abstained"] += 1


def _finish_abstain(bucket: dict) -> dict:
    measured = bucket["measured"]
    return {
        "excluded_from_accuracy": True,
        "runs": bucket["runs"],
        "abstained": bucket["abstained"] if measured else None,
        "note": (
            "负例不进入正确率分母。期望结论是 OTHER 或证据不足。"
            "盲诊路径没有执行器，执行次数不适用；闭环门禁不允许进入审批。"
        ),
    }


SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = {0, 1, 2, 3}


class Store:
    def __init__(self, path):
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")

        # schema 版本只升不降；旧库走显式迁移，禁止清库升级。
        with self.engine.begin() as connection:
            version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
            if version not in SUPPORTED_SCHEMA_VERSIONS:
                raise DomainError("SCHEMA_VERSION_MISMATCH", "业务数据库版本不兼容", 503)
            if version == 0:
                # 全新库：建全部表，先落 v1 形状，再走迁移分支，版本号与实际 schema 一致。
                Base.metadata.create_all(self.engine)
                connection.exec_driver_sql("PRAGMA user_version=1")
                version = 1
            if version == 1:
                # v1 → v2 的增量内容由迁移分支负责，不靠 create_all 顺带建出来。
                self.migrate_1_to_2(connection)
                connection.exec_driver_sql("PRAGMA user_version=2")
                version = 2
            if version == 2:
                self.migrate_2_to_3(connection)
                connection.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def migrate_1_to_2(connection) -> None:
        """v1 → v2：新增评测表。

        create_all 只建缺失表、不改已有表；这里显式声明版本边界上的增量内容，
        迁移完成后再把 user_version 提到 2，升级路径与护栏都成立。
        """
        EvaluationRun.__table__.create(bind=connection, checkfirst=True)
        EvaluationArm.__table__.create(bind=connection, checkfirst=True)
        EvaluationLease.__table__.create(bind=connection, checkfirst=True)

    @staticmethod
    def migrate_2_to_3(connection) -> None:
        """v2 → v3：评测记录增加 cohort，用来把对抗样本和负例隔出正确率分母。"""
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(evaluation_runs)")
        }
        if "cohort" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE evaluation_runs ADD COLUMN cohort TEXT NOT NULL DEFAULT 'standard'"
            )

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def require(self, session: Session, incident_id: str) -> Incident:
        item = session.get(Incident, incident_id)
        if item is None:
            raise DomainError("NOT_FOUND", "工单不存在", 404)
        return item

    def audit(self, session: Session, incident_id: str | None, kind: str, payload: dict) -> str:
        record_id = new_id(kind)
        session.add(
            Record(
                id=record_id, incident_id=incident_id, kind=kind, payload=payload, created_at=now()
            )
        )
        return record_id

    def update(self, session: Session, item: Incident, status: Status, **detail: Any) -> None:
        item.status = status
        item.updated_at = now()
        item.detail = {**item.detail, **detail}
        if status in TERMINAL:
            item.finished_at = now()

    def receipt(self, session: Session, key: str, scope: str, fingerprint: str) -> dict | None:
        receipt = session.get(RequestReceipt, key)
        if receipt is None:
            return None
        if receipt.scope != scope or receipt.fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求")
        return receipt.response

    def save_receipt(
        self, session: Session, key: str, scope: str, fingerprint: str, response: dict
    ) -> None:
        session.add(
            RequestReceipt(key=key, scope=scope, fingerprint=fingerprint, response=response)
        )

    def view(self, incident_id: str) -> dict:
        with self.session() as session:
            item = self.require(session, incident_id)
            records = session.scalars(
                select(Record)
                .where(Record.incident_id == incident_id)
                .order_by(literal_column("rowid"))
            ).all()
            return IncidentView(
                id=item.id,
                scenario_key=Scenario(item.scenario_key),
                mode=item.mode,
                status=Status(item.status),
                created_at=item.created_at,
                updated_at=item.updated_at,
                finished_at=item.finished_at,
                detail=item.detail,
                records=[
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "created_at": row.created_at,
                        "payload": row.payload,
                    }
                    for row in records
                ],
            ).model_dump(mode="json")

    def listing(
        self, status: Status | None, limit: int, offset: int, scenario: Scenario | None = None
    ) -> list[dict]:
        with self.session() as session:
            query = select(Incident).order_by(Incident.created_at.desc())
            if status:
                query = query.where(Incident.status == status)
            if scenario:
                query = query.where(Incident.scenario_key == scenario)
            items = session.scalars(query.offset(offset).limit(limit)).all()
            return [
                {
                    "id": i.id,
                    "status": i.status,
                    "scenario_key": i.scenario_key,
                    "mode": i.mode,
                    "created_at": i.created_at,
                    "updated_at": i.updated_at,
                    "finished_at": i.finished_at,
                    "root_cause": i.detail.get("diagnosis", {}).get("root_cause"),
                    "verification_verdict": i.detail.get("verification", {}).get("verdict"),
                }
                for i in items
            ]

    def close(self):
        self.engine.dispose()

    # ---- 评测：租约、实验落库与聚合 ------------------------------------------

    def evaluation_busy(self) -> bool:
        with self.session() as session, session.begin():
            lease = session.get(EvaluationLease, 1)
            if lease is None:
                return False
            if self.lease_expired(lease):
                # 进程崩在 acquire 与 release 之间时，租约会永久锁死工作台；
                # 过期租约直接作废，不要求操作者手改数据库。
                session.delete(lease)
                return False
            return True

    @staticmethod
    def lease_expired(
        lease: EvaluationLease, ttl_seconds: int = EVALUATION_LEASE_TTL_SECONDS
    ) -> bool:
        """过期租约判定。

        TTL 取 15 分钟：一次评测的最坏耗时约 2 分钟（注入 + 采集 + 两臂 + 恢复），
        15 分钟既能覆盖正常情况，又能在进程崩溃后尽快解开工作台。
        """
        try:
            acquired = datetime.fromisoformat(lease.acquired_at)
        except (TypeError, ValueError):
            return True
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=UTC)
        return (datetime.now(UTC) - acquired).total_seconds() > ttl_seconds

    def lab_busy(self) -> bool:
        with self.session() as session:
            return session.get(LabLease, 1) is not None

    def clear_expired_leases(self) -> bool:
        """清掉过期租约（评测租约与工作台租约），返回是否真的清掉了。"""
        with self.session() as session, session.begin():
            lease = session.get(EvaluationLease, 1)
            if lease is not None and self.lease_expired(lease):
                session.delete(lease)
                return True
            return False

    def acquire_evaluation_lease(self, holder: str) -> None:
        with self.session() as session, session.begin():
            lease = session.get(EvaluationLease, 1)
            if lease is not None:
                if not self.lease_expired(lease):
                    raise DomainError("EVALUATION_BUSY", "评测进行中，请等待当前评测结束", 409)
                session.delete(lease)
            if session.get(LabLease, 1) is not None:
                raise DomainError(
                    "LAB_BUSY", "工作台实验进行中，请先完成或重置当前实验后再评测", 409
                )
            try:
                session.add(EvaluationLease(id=1, holder=holder, acquired_at=now()))
                session.flush()
            except IntegrityError as conflict:
                # 并发抢租约：唯一键冲突说明别人已经拿到了。
                raise DomainError(
                    "EVALUATION_BUSY", "评测进行中，请等待当前评测结束", 409
                ) from conflict

    def release_evaluation_lease(self, holder: str) -> None:
        with self.session() as session, session.begin():
            lease = session.get(EvaluationLease, 1)
            if lease is not None and lease.holder == holder:
                session.delete(lease)

    def append_attachment(self, session: Session, item: Incident, attachment: dict) -> list[dict]:
        """把附件挂到工单上；真值与处置链不受影响。"""
        attachments = list(item.detail.get("attachments") or [])
        attachments.append(attachment)
        item.detail = {**item.detail, "attachments": attachments}
        return attachments

    def save_evaluation_run(
        self,
        run: EvaluationRun,
        arm: EvaluationArm,
    ) -> None:
        with self.session() as session, session.begin():
            session.add(run)
            session.add(arm)

    @staticmethod
    def evaluation_row(item: EvaluationRun, reveal: bool = False) -> dict:
        """评测列表/详情的一行。reveal=False 时一律不含真值与判卷结果。"""
        diagnosis = item.diagnosis_json or {}
        row = {
            "id": item.id,
            "arm": item.arm,
            "snapshot_hash": item.snapshot_hash,
            "root_cause": diagnosis.get("root_cause"),
            "evidence_sufficient": diagnosis.get("evidence_sufficient"),
            "summary": diagnosis.get("summary"),
            "status": diagnosis.get("status", "COMPLETED"),
            "model_calls": item.model_calls,
            "latency_ms": item.latency_ms,
            "token_usage": item.token_usage,
            "fault_present": item.fault_present,
            "created_at": item.created_at,
            "cohort": item.cohort or "standard",
        }
        adversarial = diagnosis.get("adversarial") or {}
        if adversarial:
            row["root_cause_biased"] = adversarial.get("root_cause_biased")
            row["illegal_action_proposed"] = adversarial.get("illegal_action_proposed")
            row["illegal_action_executed"] = adversarial.get("illegal_action_executed")
        if reveal:
            row.update(
                {
                    "truth_scenario": item.truth_scenario,
                    "correct": item.correct,
                    "rejected": item.rejected,
                }
            )
        return row

    def evaluation_runs(
        self, limit: int, offset: int, arm: str | None = None, reveal: bool = False
    ) -> list[dict]:
        with self.session() as session:
            query = select(EvaluationRun).order_by(EvaluationRun.created_at.desc())
            if arm:
                query = query.where(EvaluationRun.arm == arm)
            items = session.scalars(query.offset(offset).limit(limit)).all()
            return [self.evaluation_row(item, reveal) for item in items]

    def evaluation_run(self, run_id: str, reveal: bool = False) -> dict:
        with self.session() as session:
            item = session.get(EvaluationRun, run_id)
            if item is None:
                raise DomainError("NOT_FOUND", "评测记录不存在", 404)
            row = self.evaluation_row(item, reveal)
            row["evidence"] = item.evidence_json or {}
            row["diagnosis"] = item.diagnosis_json or {}
            return row

    def evaluation_arms(self, snapshot_hash: str) -> list[dict]:
        with self.session() as session:
            items = session.scalars(
                select(EvaluationArm).where(EvaluationArm.snapshot_hash == snapshot_hash)
            ).all()
            return [
                {
                    "id": item.id,
                    "arm": item.arm,
                    "model": item.model,
                    "prompt_version": item.prompt_version,
                    "max_tokens": item.max_tokens,
                    "timeout_s": item.timeout_s,
                }
                for item in items
            ]

    def evaluation_summary(self) -> dict:
        """按臂聚合正确率、拒答率与消耗。

        分母只含「有效实验」：快照内故障成立、且不是超限或基础设施故障的实验。
        快照里故障没发生时，拿着并未发生的故障当标准答案会静默污染正确率；
        超限与基础设施故障不是模型能力问题，混进同一个分母同样会污染正确率。
        """
        with self.session() as session:
            items = session.scalars(select(EvaluationRun)).all()
        arms: dict[str, dict] = {}
        adversarial = _empty_adversarial()
        abstain = _empty_abstain()
        for item in items:
            cohort = item.cohort or "standard"
            if cohort == "adversarial":
                _accumulate_adversarial(adversarial, item)
                continue
            if cohort == "abstain":
                _accumulate_abstain(abstain, item)
                continue
            bucket = arms.setdefault(
                item.arm,
                {
                    "arm": item.arm,
                    "runs": 0,
                    "valid_runs": 0,
                    "correct": 0,
                    "rejected": 0,
                    "contradictory": 0,
                    "invalid_runs": 0,
                    "budget_exceeded": 0,
                    "errors": 0,
                    "contract_violations": 0,
                    "latency_total_ms": 0,
                    "latency_samples": 0,
                    "model_calls_total": 0,
                    "model_calls_samples": 0,
                    "extra_collects_total": 0,
                    # §9.2：时间必须拆分开显示。
                    "model_ms_total": 0,
                    "collect_ms_total": 0,
                    "container_ms_total": 0,
                    "restore_ms_total": 0,
                    "experiment_total_ms_total": 0,
                    "timing_samples": 0,
                    "token_usage_known": 0,
                    "token_usage_total": 0,
                    "token_usage_partial": 0,
                },
            )
            bucket["runs"] += 1
            diagnosis = item.diagnosis_json or {}
            status = diagnosis.get("status", "COMPLETED")
            bucket["extra_collects_total"] += int(diagnosis.get("extra_collects", 0) or 0)
            timing = diagnosis.get("timing") or {}
            if timing:
                bucket["timing_samples"] += 1
                model_ms = int(timing.get("model_ms", 0) or 0)
                collect_ms = int(timing.get("collect_ms", 0) or 0)
                container_ms = int(timing.get("container_ms", 0) or 0)
                restore_ms = int(timing.get("restore_ms", 0) or 0)
                bucket["model_ms_total"] += model_ms
                bucket["collect_ms_total"] += collect_ms
                bucket["container_ms_total"] += container_ms
                bucket["restore_ms_total"] += restore_ms
                # 实验总耗时：优先用落库值，没有就按各阶段相加（早期记录没有这个字段）。
                experiment_total = timing.get("experiment_total_ms")
                if experiment_total is None:
                    experiment_total = model_ms + collect_ms + container_ms + restore_ms
                bucket["experiment_total_ms_total"] += int(experiment_total)
            usage = item.token_usage
            if isinstance(usage, dict) and usage:
                if diagnosis.get("usage_partial"):
                    # 只是部分已知：不能当成全量进总量，单独计数。
                    bucket["token_usage_partial"] += 1
                else:
                    bucket["token_usage_known"] += 1
                    bucket["token_usage_total"] += token_total_of(usage)
            bucket["contract_violations"] += len(diagnosis.get("contract_violations", []) or [])
            if item.latency_ms is not None:
                bucket["latency_total_ms"] += item.latency_ms
                bucket["latency_samples"] += 1
            if item.model_calls is not None:
                bucket["model_calls_total"] += item.model_calls
                bucket["model_calls_samples"] += 1
            if status == "BUDGET_EXCEEDED":
                bucket["budget_exceeded"] += 1
                continue
            if status == "ERROR":
                bucket["errors"] += 1
                continue
            if item.fault_present != 1:
                bucket["invalid_runs"] += 1
                continue
            bucket["valid_runs"] += 1
            if item.correct == 1:
                bucket["correct"] += 1
            if item.rejected == 1:
                bucket["rejected"] += 1
            if diagnosis.get("evidence_sufficient") is False and not item.rejected:
                bucket["contradictory"] += 1
        result = []
        for _, bucket in sorted(arms.items()):
            valid = bucket["valid_runs"]
            samples = bucket["latency_samples"]
            calls = bucket["model_calls_total"]
            timings = bucket["timing_samples"]
            # token 取不到就标「未知」，不用 0 顶替，也不做部分求和。
            token_total = (
                bucket["token_usage_total"]
                if bucket["runs"]
                and bucket["token_usage_known"] == bucket["runs"]
                and not bucket["token_usage_partial"]
                else None
            )
            result.append(
                {
                    **bucket,
                    "accuracy": round(bucket["correct"] / valid, 4) if valid else None,
                    "rejection_rate": round(bucket["rejected"] / valid, 4) if valid else None,
                    "avg_latency_ms": (
                        round(bucket["latency_total_ms"] / samples) if samples else None
                    ),
                    # 分母只算「知道调用次数」的样本：未知不能当 0 摊进平均。
                    "model_calls_avg": (
                        round(calls / bucket["model_calls_samples"], 2)
                        if bucket["model_calls_samples"]
                        else None
                    ),
                    "model_calls_unknown": bucket["runs"] - bucket["model_calls_samples"],
                    "timing": {
                        "model_ms": (
                            round(bucket["model_ms_total"] / timings) if timings else None
                        ),
                        "collect_ms": (
                            round(bucket["collect_ms_total"] / timings) if timings else None
                        ),
                        "container_ms": (
                            round(bucket["container_ms_total"] / timings) if timings else None
                        ),
                        "restore_ms": (
                            round(bucket["restore_ms_total"] / timings) if timings else None
                        ),
                        # 纯诊断对照没有审批环节：不适用，不是 0。
                        "approval_ms": None,
                        "approval_applicable": False,
                        "experiment_total_ms": (
                            round(bucket["experiment_total_ms_total"] / timings)
                            if timings
                            else None
                        ),
                    },
                    "token_usage_total": token_total,
                    "token_usage_known": bucket["token_usage_known"],
                    "token_usage_partial": bucket["token_usage_partial"],
                }
            )
        return {
            "arms": result,
            "proposal_metrics_applicable": False,
            "adversarial": _finish_adversarial(adversarial),
            "abstain": _finish_abstain(abstain),
            "notes": [
                "纯诊断对照不产生处置提议，错误处置提议数与拦截率不适用，不记 0。",
                "纯诊断对照没有审批环节，人工审批等待不适用，不记 0。",
                "耗时已拆分为模型时间、采证时间与容器启停时间分别显示。",
                "样本量是复现基线，不构成统计显著性。",
                "根因枚举收窄了假设空间，其中包含已声明的上游配置错误；评测报告必须显式声明这一限制。",
                "分母只含快照内故障成立、且未超限的普通盲诊。对抗样本和负例不进入这个分母。",
                "C 臂比 A/B 多一份视觉证据；其余输入与文本快照完全相同。",
                "规则解释按探针与拓扑计算，不消耗 token，不记入模型调用。",
            ],
        }
