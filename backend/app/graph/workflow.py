import json
import logging
import time
from collections.abc import Callable
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.evidence import EvidenceAgent, observations_available, validate_diagnosis
from app.config import DomainError
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
    ) -> None:
        self.store, self.evidence_agent = store, evidence_agent
        self.diagnosis, self.planner = diagnosis, planner
        self.executor, self.probes = executor, probes
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
        builder.add_conditional_edges("diagnose", lambda s: END if s.get("error") else "plan")
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
            items = self.evidence_agent.collect(Scenario(state.get("scenario_key", "MYSQL_DOWN")))
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
            validate_diagnosis(
                result,
                [Evidence.model_validate(e) for e in state["evidence"]],
                Scenario(state.get("scenario_key", "MYSQL_DOWN")),
            )
            return {"diagnosis": result.model_dump(mode="json")}

        return self.guarded(state, Status.DIAGNOSING, operation)

    def plan(self, state):
        def operation():
            result = self.planner.plan(
                {
                    "diagnosis": state["diagnosis"],
                    "scenario_key": state.get("scenario_key", "MYSQL_DOWN"),
                    "evidence": state["evidence"],
                    "previous_verification": state.get("verification"),
                    "previous_execution": state.get("execution"),
                }
            )
            validate_plan(result, Scenario(state.get("scenario_key", "MYSQL_DOWN")))
            plan_id = new_id("plan")
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
        if verdict == Verdict.FAIL and state["attempt"] < 2:
            return "collect_retry"
        self.phase(
            state,
            {
                Verdict.PASS: Status.RESOLVED,
                Verdict.FAIL: Status.FAILED,
                Verdict.UNKNOWN: Status.UNKNOWN,
            }[verdict],
        )
        return END
