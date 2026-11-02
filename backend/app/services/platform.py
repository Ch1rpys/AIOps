import logging
import threading
import time

import httpx
from langgraph.types import Command
from sqlalchemy import select

from app.agents.evidence import fault_observed
from app.config import SERVICES, DomainError
from app.repositories.store import Incident, LabLease, RequestReceipt
from app.scenarios import SCENARIOS
from app.schemas.domain import TERMINAL, Scenario, Status, new_id, now
from app.services.lab import lab_baseline

logger = logging.getLogger("aiops.platform")
RUNNING = {
    Status.CREATED,
    Status.COLLECTING,
    Status.DIAGNOSING,
    Status.PLANNING,
    Status.EXECUTING,
    Status.VERIFYING,
}


class Platform:
    def __init__(self, settings, store, docker, probes, evidence, workflow, client, mode="REAL"):
        self.settings, self.store, self.docker = settings, store, docker
        self.probes, self.evidence, self.workflow = probes, evidence, workflow
        self.client, self.mode = client, mode
        self.lock = threading.RLock()

    def recover(self):
        with self.store.session() as session, session.begin():
            items = session.scalars(
                select(Incident).where(
                    Incident.status.in_([s.value for s in RUNNING | {Status.WAITING_APPROVAL}])
                )
            ).all()
            for item in items:
                checkpoint = self.workflow.graph.get_state(self.graph_config(item.id))
                waiting = any(task.interrupts for task in checkpoint.tasks)
                approvals = session.scalars(
                    select(RequestReceipt).where(RequestReceipt.scope == f"approval:{item.id}")
                ).all()
                pending = any(
                    r.response.get("plan_id") == item.detail.get("plan_id") for r in approvals
                )
                if item.status == Status.WAITING_APPROVAL and waiting and not pending:
                    continue
                # 运行中崩溃不自动重放容器操作；等待审批仅从有效检查点恢复。
                self.store.update(
                    session,
                    item,
                    Status.ERROR,
                    error={
                        "code": "PROCESS_INTERRUPTED",
                        "message": "上次进程中断，请检查证据并重置环境",
                    },
                )
                self.store.audit(session, item.id, "recovery", item.detail["error"])

    @staticmethod
    def graph_config(incident_id: str) -> dict:
        return {"configurable": {"thread_id": incident_id}, "recursion_limit": 30}

    def environment(self) -> dict:
        baseline = lab_baseline(self.docker, self.probes)
        with self.store.session() as session:
            lease = session.get(LabLease, 1)
            owner = lease.incident_id if lease else None
        return {**baseline, "active_incident_id": owner, "captured_at": now()}

    def health(self) -> dict:
        try:
            response = self.client.get(
                self.settings.holmes_url + "/healthz", timeout=self.settings.probe_timeout
            )
            holmes = {
                "status": "PASS" if response.status_code == 200 else "FAIL",
                "http_status": response.status_code,
            }
        except httpx.HTTPError:
            holmes = {"status": "FAIL", "reason": "AI_UNAVAILABLE"}
        configured = bool(
            self.settings.planner_url
            and self.settings.planner_model
            and self.settings.planner_api_key.get_secret_value()
        )
        return {
            "platform": "PASS",
            "mode": self.mode,
            "holmes": holmes,
            "planner": {
                "status": "UNKNOWN",
                "configured": configured,
                "reason": "须由真实模型调用验证连通性",
            },
            "admin_configured": bool(self.settings.admin_token.get_secret_value()),
            "lab": self.environment(),
        }

    def replay(self, response):
        if response is None:
            return None
        if response.get("error"):
            error = response["error"]
            raise DomainError(error["code"], error["message"], error["status"])
        if response.get("pending"):
            raise DomainError("OPERATION_UNCERTAIN", "前次操作结果未知，请检查并重置环境")
        return response

    def inject(self, scenario: Scenario, key: str) -> dict:
        scope, fingerprint = "inject", scenario.value
        with self.lock:
            with self.store.session() as session:
                previous = self.replay(self.store.receipt(session, key, scope, fingerprint))
                if previous:
                    return previous
                if session.get(LabLease, 1):
                    raise DomainError("LAB_BUSY", "实验环境已被占用，请先完成或重置当前实验")
            # 评测与工作台共用同一个四容器环境，不能并行。
            if self.store.evaluation_busy():
                raise DomainError("EVALUATION_BUSY", "评测进行中，请等待评测结束后再注入故障", 409)
            baseline = self.environment()
            if not baseline["healthy"]:
                raise DomainError("BASELINE_UNHEALTHY", "四个 demo 服务与探针未全部通过，禁止注入")
            incident_id = new_id("inc")
            with self.store.session() as session, session.begin():
                item = Incident(
                    id=incident_id,
                    scenario_key=scenario,
                    mode=self.mode,
                    status=Status.CREATED,
                    created_at=now(),
                    updated_at=now(),
                    detail={
                        "baseline": baseline,
                        "fault_confirmed": False,
                        "integration_mode": "evidence_context",
                        "diagnosis_provider": type(self.workflow.diagnosis).__name__,
                        "planner_provider": type(self.workflow.planner).__name__,
                        "holmes_model": self.settings.holmes_model or "service_default",
                        "planner_model": self.settings.planner_model or "not_configured",
                    },
                )
                session.add(item)
                session.flush()
                session.add(LabLease(id=1, incident_id=incident_id))
                self.store.save_receipt(session, key, scope, fingerprint, {"pending": True})
            try:
                output = self.docker.change(SCENARIOS[scenario].target, "stop")
                evidence = []
                confirmed = False
                # Docker 退出后代理可能短暂返回 504；有界补采，不把它误认成目标 502。
                for index in range(self.settings.fault_confirmation_attempts):
                    if index:
                        time.sleep(self.settings.fault_confirmation_interval)
                    sample = self.evidence.collect(scenario)
                    evidence.extend(sample)
                    confirmed = fault_observed(sample, scenario)
                    if confirmed:
                        break
                with self.store.session() as session, session.begin():
                    item = self.store.require(session, incident_id)
                    self.store.audit(session, incident_id, "injection", output)
                    for entry in evidence:
                        self.store.audit(
                            session, incident_id, "evidence", entry.model_dump(mode="json")
                        )
                    self.store.update(
                        session,
                        item,
                        Status.INJECTED if confirmed else Status.UNKNOWN,
                        fault_confirmed=confirmed,
                    )
                if not confirmed:
                    raise DomainError(
                        "FAULT_NOT_CONFIRMED", "停止操作后未取得场景必需故障证据", 502
                    )
                response = {
                    "incident_id": incident_id,
                    "scenario_key": scenario,
                    "status": Status.INJECTED,
                    "fault_confirmed": True,
                }
            except Exception as failure:
                error = (
                    failure
                    if isinstance(failure, DomainError)
                    else DomainError(
                        "INJECTION_ERROR", "注入过程中发生异常，请检查工单并重置环境", 500
                    )
                )
                with self.store.session() as session, session.begin():
                    item = self.store.require(session, incident_id)
                    self.store.update(
                        session,
                        item,
                        Status.ERROR,
                        error={"code": error.code, "message": error.message},
                    )
                    session.get(RequestReceipt, key).response = {
                        "error": {
                            "code": error.code,
                            "message": error.message,
                            "status": error.status,
                        }
                    }
                raise error from None
            with self.store.session() as session, session.begin():
                session.get(RequestReceipt, key).response = response
            return response

    def start(self, incident_id: str) -> dict:
        with self.lock, self.store.session() as session, session.begin():
            item = self.store.require(session, incident_id)
            if item.status != Status.INJECTED:
                raise DomainError("INVALID_STATE", "仅已确认注入的工单可开始诊断")
            run_id = new_id("run")
            self.store.update(
                session, item, Status.COLLECTING, run_id=run_id, attempt=1, error=None
            )
            self.store.audit(
                session, incident_id, "run", {"run_id": run_id, "thread_id": incident_id}
            )
            return {"incident_id": incident_id, "run_id": run_id}

    def approve(self, incident_id, request, key):
        scope = f"approval:{incident_id}"
        fingerprint = request.model_dump_json()
        with self.lock, self.store.session() as session, session.begin():
            response = self.replay(self.store.receipt(session, key, scope, fingerprint))
            if response:
                return response, False
            item = self.store.require(session, incident_id)
            if (
                item.status != Status.WAITING_APPROVAL
                or item.detail.get("plan_id") != request.plan_id
            ):
                raise DomainError("INVALID_STATE", "工单未等待此计划审批，或计划已过期")
            existing = session.scalars(
                select(RequestReceipt).where(RequestReceipt.scope == scope)
            ).all()
            if any(r.response.get("plan_id") == request.plan_id for r in existing):
                raise DomainError("ALREADY_DECIDED", "此计划已审批")
            checkpoint = self.workflow.graph.get_state(self.graph_config(incident_id))
            if not any(task.interrupts for task in checkpoint.tasks):
                raise DomainError("CHECKPOINT_NOT_READY", "审批检查点尚未就绪，请稍后刷新")
            response = {
                "incident_id": incident_id,
                "plan_id": request.plan_id,
                "decision": request.decision,
                "accepted": True,
            }
            self.store.audit(
                session,
                incident_id,
                "approval",
                {**request.model_dump(), "operator": "local-admin", "idempotency_key": key},
            )
            self.store.save_receipt(session, key, scope, fingerprint, response)
            self.store.update(session, item, Status.EXECUTING, approval=response)
            return response, True

    def run(self, incident_id: str, resume: dict | None = None):
        try:
            item = self.store.view(incident_id)
            initial = (
                Command(resume=resume)
                if resume
                else {
                    "incident_id": incident_id,
                    "run_id": item["detail"]["run_id"],
                    "attempt": 1,
                    "scenario_key": item["scenario_key"],
                    "collect_count": 0,
                    "supplement_count": 0,
                    "supplements": [],
                    "started_at": now(),
                    "error": None,
                }
            )
            self.workflow.graph.invoke(initial, self.graph_config(incident_id))
        except Exception as error:
            logger.error("workflow_error incident_id=%s type=%s", incident_id, type(error).__name__)
            code = error.code if isinstance(error, DomainError) else "WORKFLOW_ERROR"
            message = (
                error.message
                if isinstance(error, DomainError)
                else "流程异常，请查看本机日志并重置"
            )
            with self.store.session() as session, session.begin():
                item = self.store.require(session, incident_id)
                self.store.update(
                    session, item, Status.ERROR, error={"code": code, "message": message}
                )
                self.store.audit(session, incident_id, "error", {"code": code, "message": message})
        finally:
            with self.store.session() as session, session.begin():
                item = self.store.require(session, incident_id)
                if item.status == Status.RESOLVED:
                    lease = session.get(LabLease, 1)
                    if lease and lease.incident_id == incident_id:
                        session.delete(lease)

    def reset(self, key: str) -> dict:
        with self.lock:
            with self.store.session() as session:
                previous = self.replay(self.store.receipt(session, key, "reset", "confirmed"))
                if previous:
                    return previous
                lease = session.get(LabLease, 1)
                incident_id = lease.incident_id if lease else None
                if incident_id and self.store.require(session, incident_id).status in RUNNING:
                    raise DomainError("LAB_BUSY", "流程运行中，暂不能重置")
            if self.store.evaluation_busy():
                raise DomainError("EVALUATION_BUSY", "评测进行中，请等待评测结束后再重置", 409)
            with self.store.session() as session, session.begin():
                self.store.save_receipt(session, key, "reset", "confirmed", {"pending": True})
                self.store.audit(
                    session, incident_id, "reset", {"state": "STARTED", "operator": "local-admin"}
                )
            try:
                outputs = [self.docker.change(service, "start") for service in SERVICES]
                for index in range(self.settings.verification_attempts):
                    if index:
                        time.sleep(self.settings.verification_interval)
                    environment = self.environment()
                    if environment["healthy"]:
                        break
                if not environment["healthy"]:
                    raise DomainError("RESET_FAILED", "重置后基线未全部通过", 502)
            except Exception as failure:
                error = (
                    failure
                    if isinstance(failure, DomainError)
                    else DomainError(
                        "RESET_FAILED", "重置过程中发生异常，实验锁保留，请检查环境", 500
                    )
                )
                with self.store.session() as session, session.begin():
                    self.store.audit(
                        session, incident_id, "reset", {"state": "FAILED", "code": error.code}
                    )
                    session.get(RequestReceipt, key).response = {
                        "error": {
                            "code": error.code,
                            "message": error.message,
                            "status": error.status,
                        }
                    }
                raise error from None
            with self.store.session() as session, session.begin():
                lease = session.get(LabLease, 1)
                if lease:
                    item = self.store.require(session, lease.incident_id)
                    if item.status not in TERMINAL:
                        self.store.update(session, item, Status.CANCELLED, reset=True)
                    session.delete(lease)
                self.store.audit(
                    session,
                    incident_id,
                    "reset",
                    {"state": "SUCCEEDED", "outputs": outputs, "baseline": environment},
                )
                response = {"status": "PASS", "baseline": environment}
                session.get(RequestReceipt, key).response = response
            return response
