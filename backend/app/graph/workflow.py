import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.evidence import (
    EvidenceAgent,
    observations_available,
    validate_evidence_integrity,
    validate_execution_preconditions,
)
from app.config import CHECK_IDS, DomainError, Settings
from app.integrations.models import DiagnosisProvider, PlannerProvider
from app.integrations.probes import Probes
from app.repositories.store import Store
from app.schemas.domain import Evidence, Plan, Scenario, Status, Verdict, new_id, now
from app.services.executor import ActionExecutor, validate_plan

logger = logging.getLogger("aiops.workflow")


class IncidentState(TypedDict, total=False):
    scenario_key: str
    incident_id: str
    run_id: str
    attempt: int
    collect_count: int
    supplement_count: int
    supplements: list[dict]
    started_at: str
    evidence: list[dict]
    diagnosis: dict
    plan: dict
    plan_id: str
    decision: str
    verification: dict
    execution: dict
    error: dict | None


class Workflow:
    def __init__(
        self,
        store: Store,
        evidence_agent: EvidenceAgent,
        diagnosis: DiagnosisProvider,
        planner: PlannerProvider,
        executor: ActionExecutor,
        probes: Probes,
        checkpointer: BaseCheckpointSaver,
        settings: Settings | None = None,
    ) -> None:
        self.store, self.evidence_agent = store, evidence_agent
        self.diagnosis, self.planner = diagnosis, planner
        self.executor, self.probes = executor, probes
        self.settings = settings or evidence_agent.settings
        builder = StateGraph(IncidentState)
        for name, node in (
            ("collect", self.collect),
            ("diagnose", self.diagnose),
            ("plan", self.plan),
            ("approval", self.approval),
            ("execute", self.execute),
            ("verify", self.verify),
        ):
            builder.add_node(name, node)
        builder.add_edge(START, "collect")
        builder.add_node("collect_retry", self.collect_retry)
        builder.add_edge("collect_retry", "collect")
        builder.add_conditional_edges("collect", self.after_collect)
        # M3：诊断可以请求补采，补采后回到诊断节点重来一轮。
        builder.add_node("supplement", self.supplement)
        builder.add_edge("supplement", "diagnose")
        builder.add_conditional_edges("diagnose", self.after_diagnose)
        builder.add_conditional_edges("plan", lambda s: END if s.get("error") else "approval")
        builder.add_conditional_edges(
            "approval", lambda s: "execute" if s["decision"] == "approve" else END
        )
        builder.add_conditional_edges("execute", lambda s: END if s.get("error") else "verify")
        builder.add_conditional_edges("verify", self.after_verify)
        self.graph = builder.compile(checkpointer=checkpointer)

    def collect_retry(self, state):
        return {"attempt": state["attempt"] + 1, "collect_count": 0, "decision": "", "error": None}

    def phase(self, state: IncidentState, status: Status, **detail):
        with self.store.session() as session, session.begin():
            item = self.store.require(session, state["incident_id"])
            self.store.update(session, item, status, attempt=state["attempt"], **detail)

    def guarded(self, state: IncidentState, phase: Status, operation: Callable[[], dict]) -> dict:
        started_at, start = now(), time.monotonic()
        self.phase(state, phase)
        try:
            result = operation()
            outcome = "SUCCEEDED"
        except Exception as failure:
            if isinstance(failure, DomainError):
                error = failure
            elif phase == Status.VERIFYING:
                error = DomainError("PROBE_UNAVAILABLE", "探针执行失败，未取得有效观测", 503)
            else:
                error = DomainError("WORKFLOW_ERROR", "节点运行异常，请检查证据并重置环境", 500)
            terminal = (
                Status.BLOCKED
                if error.code == "BLOCKED"
                else (
                    Status.UNKNOWN
                    if error.code
                    in {"EVIDENCE_INSUFFICIENT", "EVIDENCE_CONFLICT", "PROBE_UNAVAILABLE"}
                    else Status.ERROR
                )
            )
            result = {"error": {"code": error.code, "message": error.message}}
            if error.details:
                result["error"]["details"] = error.details
            if error.code == "PROBE_UNAVAILABLE":
                result["verification"] = {
                    "verdict": Verdict.UNKNOWN,
                    "samples": [],
                    "captured_at": now(),
                }
            self.phase(state, terminal, **result)
            outcome = "FAILED"
        elapsed = round((time.monotonic() - start) * 1000)
        record = {
            "run_id": state["run_id"],
            "attempt": state["attempt"],
            "phase": phase,
            "status": outcome,
            "started_at": started_at,
            "ended_at": now(),
            "elapsed_ms": elapsed,
            "output": result,
            "input_digest": {"evidence_ids": [e["id"] for e in state.get("evidence", [])]},
        }
        with self.store.session() as session, session.begin():
            self.store.audit(session, state["incident_id"], "step", record)
        logger.info(
            json.dumps(
                {
                    "incident_id": state["incident_id"],
                    "run_id": state["run_id"],
                    "phase": phase,
                    "result": outcome,
                    "elapsed_ms": elapsed,
                }
            )
        )
        return result

    def collect(self, state: IncidentState):
        def operation():
            items = self.evidence_agent.collect(
                Scenario(state.get("scenario_key", "MYSQL_DOWN")),
                attachments=self.attachments_of(state),
            )
            with self.store.session() as session, session.begin():
                for item in items:
                    self.store.audit(
                        session, state["incident_id"], "evidence", item.model_dump(mode="json")
                    )
            return {
                "evidence": [i.model_dump(mode="json") for i in items],
                "collect_count": state.get("collect_count", 0) + 1,
            }

        return self.guarded(state, Status.COLLECTING, operation)

    def attachments_of(self, state) -> list[dict]:
        """工单挂载的截图附件。"""
        with self.store.session() as session:
            item = self.store.require(session, state["incident_id"])
            return list(item.detail.get("attachments") or [])

    def after_collect(self, state):
        if state.get("error"):
            return END
        # 第二轮容器可能已运行；仍需把失败业务探针交给诊断，再校验根因是否支持动作。
        if observations_available(
            [Evidence.model_validate(e) for e in state["evidence"]],
            Scenario(state.get("scenario_key", "MYSQL_DOWN")),
        ):
            return "diagnose"
        if state["collect_count"] < 2:
            return "collect"
        self.phase(
            state,
            Status.UNKNOWN,
            error={"code": "EVIDENCE_INSUFFICIENT", "message": "补采一次后仍未取得一致的故障证据"},
        )
        return END

    def diagnose(self, state):
        def operation():
            # 截图与实测证据冲突时先补查：图片本身不能决定恢复，也不能放行到审批页。
            conflict = self.attachment_conflict(state)
            if conflict:
                raise DomainError("EVIDENCE_CONFLICT", conflict)
            result = self.diagnosis.diagnose(
                {
                    "incident_id": state["incident_id"],
                    "scenario_key": state.get("scenario_key", "MYSQL_DOWN"),
                    "attempt": state["attempt"],
                    "evidence": state["evidence"],
                    "previous_verification": state.get("verification"),
                    "previous_execution": state.get("execution"),
                }
            )
            # 语义校验拦截前保存模型结论，UNKNOWN 也必须能回溯其依据与疑点。
            self.phase(state, Status.DIAGNOSING, diagnosis=result.model_dump(mode="json"))
            # 闭环入口同时要求引用完整性与执行许可；盲诊路径只查完整性，另走独立入口。
            collected = [Evidence.model_validate(e) for e in state["evidence"]]
            validate_evidence_integrity(result, collected)
            validate_execution_preconditions(
                result,
                collected,
                Scenario(state.get("scenario_key", "MYSQL_DOWN")),
            )
            return {"diagnosis": result.model_dump(mode="json")}

        return self.guarded(state, Status.DIAGNOSING, operation)

    # ---- M3：受限主动补采 ---------------------------------------------------

    def supplement(self, state):
        """按模型请求补采观测。

        只能补采注册过的检查 ID，且不重复采集已有证据；
        轮次与总耗时都由配置封顶，超限就不再补采并如实记录原因。
        """

        def operation():
            requested = self.pending_requests(state)
            collected = []
            for request in requested:
                probe = self.probes.by_name(request["check_id"])
                item = Evidence(source=probe.name, kind="probe", raw=probe.model_dump(mode="json"))
                collected.append(item)
            with self.store.session() as session, session.begin():
                for item in collected:
                    self.store.audit(
                        session, state["incident_id"], "evidence", item.model_dump(mode="json")
                    )
            record = {
                "round": state.get("supplement_count", 0) + 1,
                "requested": requested,
                "collected": [item.model_dump(mode="json") for item in collected],
                "collected_at": now(),
            }
            self.phase(state, Status.COLLECTING, supplement=record)
            return {
                "evidence": [
                    *state["evidence"],
                    *[item.model_dump(mode="json") for item in collected],
                ],
                "collect_count": state.get("collect_count", 0) + 1,
                "supplement_count": state.get("supplement_count", 0) + 1,
                "supplements": [*(state.get("supplements") or []), record],
            }

        return self.guarded(state, Status.COLLECTING, operation)

    def pending_requests(self, state) -> list[dict]:
        """本轮真正要补采的检查：只认注册 ID，且不采已有证据。"""
        requested = (state.get("diagnosis") or {}).get("requested_checks") or []
        collected = {
            item["source"] for item in state.get("evidence", []) if item.get("kind") == "probe"
        }
        pending = []
        for request in requested:
            check_id = request.get("check_id")
            if check_id not in CHECK_IDS or check_id in collected:
                continue
            pending.append({"check_id": check_id, "reason": request.get("reason", "")})
            if len(pending) >= self.settings.supplement_max_checks:
                break
        return pending

    def after_diagnose(self, state):
        if state.get("error"):
            return END
        requested = (state.get("diagnosis") or {}).get("requested_checks") or []
        if not requested:
            return "plan"
        rounds = state.get("supplement_count", 0)
        if rounds >= self.settings.supplement_max_rounds:
            self.phase(state, Status.DIAGNOSING, supplement_stopped="已达补采轮次上限")
            return "plan"
        if self.elapsed_seconds(state) >= self.settings.supplement_budget_seconds:
            self.phase(state, Status.DIAGNOSING, supplement_stopped="补采耗时预算已用尽")
            return "plan"
        if not self.pending_requests(state):
            self.phase(state, Status.DIAGNOSING, supplement_stopped="请求的检查已全部采集或未注册")
            return "plan"
        return "supplement"

    @staticmethod
    def elapsed_seconds(state) -> float:
        started = state.get("started_at")
        if not started:
            return 0.0
        try:
            return (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()
        except ValueError:
            return 0.0

    def plan(self, state):
        def operation():
            # 截图与当前探针冲突时先补查，不进入恢复审批。
            conflict = self.attachment_conflict(state)
            if conflict:
                raise DomainError("EVIDENCE_CONFLICT", conflict)
            result = self.planner.plan(
                {
                    "diagnosis": state["diagnosis"],
                    "scenario_key": state.get("scenario_key", "MYSQL_DOWN"),
                    "evidence": state["evidence"],
                    "previous_verification": state.get("verification"),
                    "previous_execution": state.get("execution"),
                }
            )
            plan_id = new_id("plan")
            # 先留痕再校验：被白名单门禁拦下的非法提议也必须计数（§9.1「即使被拦截也要计数」）。
            with self.store.session() as session, session.begin():
                self.store.audit(
                    session,
                    state["incident_id"],
                    "plan_proposal",
                    {
                        "plan_id": plan_id,
                        "attempt": state["attempt"],
                        **result.model_dump(mode="json"),
                    },
                )
            validate_plan(result, Scenario(state.get("scenario_key", "MYSQL_DOWN")))
            self.phase(
                state, Status.WAITING_APPROVAL, plan=result.model_dump(mode="json"), plan_id=plan_id
            )
            with self.store.session() as session, session.begin():
                self.store.audit(
                    session,
                    state["incident_id"],
                    "plan",
                    {
                        "plan_id": plan_id,
                        "attempt": state["attempt"],
                        **result.model_dump(mode="json"),
                    },
                )
            return {"plan": result.model_dump(mode="json"), "plan_id": plan_id}

        return self.guarded(state, Status.PLANNING, operation)

    @staticmethod
    def attachment_conflict(state) -> str | None:
        """附件与实测证据冲突时给出明确提示；图片本身不决定恢复。"""
        for item in state.get("evidence", []):
            if item.get("kind") != "attachment":
                continue
            note = (item.get("raw") or {}).get("note", "")
            if note.startswith("时间或证据冲突"):
                return note
        return None

    def approval(self, state):
        # 恢复时会重放本节点；中断之前没有写入或外部副作用。
        response = interrupt(
            {"plan_id": state["plan_id"], "plan": state["plan"], "attempt": state["attempt"]}
        )
        if response.get("plan_id") != state["plan_id"]:
            raise DomainError("BLOCKED", "审批对应计划不匹配")
        decision = response["decision"]
        self.phase(state, Status.EXECUTING if decision == "approve" else Status.CANCELLED)
        with self.store.session() as session, session.begin():
            self.store.audit(
                session,
                state["incident_id"],
                "step",
                {
                    "run_id": state["run_id"],
                    "attempt": state["attempt"],
                    "phase": "APPROVAL",
                    "status": "SUCCEEDED",
                    "started_at": now(),
                    "ended_at": now(),
                    "elapsed_ms": 0,
                    "output": {"decision": decision, "plan_id": state["plan_id"]},
                },
            )
        return {"decision": decision}

    def execute(self, state):
        return self.guarded(
            state,
            Status.EXECUTING,
            lambda: {
                "execution": self.executor.execute(
                    state["incident_id"], state["plan_id"], Plan.model_validate(state["plan"])
                )
            },
        )

    def verify(self, state):
        def operation():
            def on_sample(probes):
                with self.store.session() as session, session.begin():
                    self.store.audit(
                        session,
                        state["incident_id"],
                        "probe_sample",
                        {
                            "attempt": state["attempt"],
                            "probes": [p.model_dump(mode="json") for p in probes],
                        },
                    )

            result = self.probes.verify(
                on_sample, Scenario(state.get("scenario_key", "MYSQL_DOWN"))
            ).model_dump(mode="json")
            with self.store.session() as session, session.begin():
                self.store.audit(
                    session,
                    state["incident_id"],
                    "verification",
                    {"attempt": state["attempt"], **result},
                )
            self.phase(state, Status.VERIFYING, verification=result)
            return {"verification": result}

        return self.guarded(state, Status.VERIFYING, operation)

    def after_verify(self, state):
        if state.get("error"):
            return END
        verdict = state["verification"]["verdict"]
        window = state["verification"].get("window") or {}
        # M5：窗口内再次失败说明恢复不稳定，不能算已恢复；
        # 窗口默认关闭时 instability 恒为 False，判定与现状逐字一致。
        unstable = bool(window.get("instability"))
        effective = Verdict.FAIL if (verdict == Verdict.PASS and unstable) else verdict
        if effective == Verdict.FAIL and state["attempt"] < 2:
            return "collect_retry"
        self.phase(
            state,
            {
                Verdict.PASS: Status.RESOLVED,
                Verdict.FAIL: Status.FAILED,
                Verdict.UNKNOWN: Status.UNKNOWN,
            }[effective],
        )
        return END
