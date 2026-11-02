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


class EvaluationArm(Base):
    """同一 snapshot 下各臂的配置快照，保证 A/B/C 的可比性可复查。"""

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


SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {0, 1, 2}


class Store:
    def __init__(self, path):
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")

        # 仅创建缺失表；schema v1 的增量迁移须新增版本，禁止清库升级。
        with self.engine.begin() as connection:
            version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
            if version not in SUPPORTED_SCHEMA_VERSIONS:
                raise DomainError("SCHEMA_VERSION_MISMATCH", "业务数据库版本不兼容", 503)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
            if version == 0:
                # 全新库：先落 v1 形状，再走迁移分支，版本号始终与实际 schema 一致。
                connection.exec_driver_sql("PRAGMA user_version=1")
                version = 1
            if version == 1:
                self.migrate_1_to_2(connection)
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
        with self.session() as session:
            return session.get(EvaluationLease, 1) is not None

    def lab_busy(self) -> bool:
        with self.session() as session:
            return session.get(LabLease, 1) is not None

    def acquire_evaluation_lease(self, holder: str) -> None:
        with self.session() as session, session.begin():
            if session.get(EvaluationLease, 1) is not None:
                raise DomainError("EVALUATION_BUSY", "评测进行中，请等待当前评测结束", 409)
            if session.get(LabLease, 1) is not None:
                raise DomainError(
                    "LAB_BUSY", "工作台实验进行中，请先完成或重置当前实验后再评测", 409
                )
            session.add(EvaluationLease(id=1, holder=holder, acquired_at=now()))

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
        }
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
        for item in items:
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
                    "extra_collects_total": 0,
                },
            )
            bucket["runs"] += 1
            diagnosis = item.diagnosis_json or {}
            status = diagnosis.get("status", "COMPLETED")
            bucket["extra_collects_total"] += int(diagnosis.get("extra_collects", 0) or 0)
            bucket["contract_violations"] += len(diagnosis.get("contract_violations", []) or [])
            if item.latency_ms is not None:
                bucket["latency_total_ms"] += item.latency_ms
                bucket["latency_samples"] += 1
            if item.model_calls is not None:
                bucket["model_calls_total"] += item.model_calls
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
            result.append(
                {
                    **bucket,
                    "accuracy": round(bucket["correct"] / valid, 4) if valid else None,
                    "rejection_rate": round(bucket["rejected"] / valid, 4) if valid else None,
                    "avg_latency_ms": (
                        round(bucket["latency_total_ms"] / samples) if samples else None
                    ),
                    "model_calls_avg": round(calls / bucket["runs"], 2) if bucket["runs"] else None,
                }
            )
        return {
            "arms": result,
            "proposal_metrics_applicable": False,
            "notes": [
                "纯诊断对照不产生处置提议，错误处置提议数与拦截率不适用，不记 0。",
                "纯诊断对照没有审批环节，人工审批等待不适用，不记 0。",
                "样本量是复现基线，不构成统计显著性。",
                "根因枚举收窄了假设空间，评测报告必须显式声明这一限制。",
                "分母只含快照内故障成立、且未超限的实验。",
            ],
        }
