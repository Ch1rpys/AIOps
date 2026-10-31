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
            if version not in {0, 1}:
                raise DomainError("SCHEMA_VERSION_MISMATCH", "业务数据库版本不兼容", 503)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA user_version=1")

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
