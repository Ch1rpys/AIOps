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
    remaining_fault,
    validate_evidence_integrity,
    validate_execution_preconditions,
)
from app.config import CHECK_IDS, DomainError, Settings
from app.integrations.models import DiagnosisProvider, ModelTrace, PlannerProvider
from app.integrations.probes import Probes
from app.repositories.store import Store
from app.scenarios import SCENARIOS
from app.schemas.domain import (
    Diagnosis,
    Evidence,
    Plan,
    Scenario,
    Status,
    Verdict,
    new_id,
    now,
)
from app.services.adversarial import apply_poison
from app.services.executor import ActionExecutor, validate_plan
from app.services.repair import (
    DIAGNOSIS_ATTEMPTS,
    PLAN_ATTEMPTS,
    probes_support_registered_fix,
    registered_diagnosis,
    registered_plan,
    repair_feedback,
)
from app.services.topology import contradicts, explain_from_evidence

logger = logging.getLogger("aiops.workflow")


class IncidentState(TypedDict, total=False):
    scenario_key: str
    incident_id: str
    run_id: str
    attempt: int
    collect_count: int
    supplement_count: int
    supplements: list[dict]
    attachment_recheck_count: int
    attachment_recheck_pending: bool
    topology_recheck_count: int
    topology_recheck_pending: bool
    topology_recheck_checks: list[str]
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
        # 执行许可校验放在补采之后：模型请求补采时必然自报「证据不足」，
        # 若在诊断节点就校验，补采永远等不到机会（§6.2）。
        builder.add_node("authorize", self.authorize)
        builder.add_conditional_edges("authorize", self.after_authorize)
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

    def guarded(
        self,
        state: IncidentState,
        phase: Status,
        operation: Callable[[], dict],
        label: str | None = None,
    ) -> dict:
        """label 用于把「同一状态下的不同步骤」在审计记录里区分开。

        例如执行许可校验与诊断都发生在 DIAGNOSING 状态，
        但只有诊断那一步真的调用了模型，统计模型调用次数时不能把门禁算进去。
        标签单独记在 step_kind 里，phase 保持不变 ——
        前端的六个阶段是按 phase 渲染的，门禁失败要能显示在「诊断」这一格里。
        """
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
            "step_kind": label or phase,
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
            scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
            with self.store.session() as session:
                poisoned = bool(
                    self.store.require(session, state["incident_id"]).detail.get("poison")
                )
            items = self.evidence_agent.collect(
                scenario,
                attachments=self.attachments_of(state),
            )
            if poisoned:
                items = apply_poison(items, scenario)
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
        if state.get("attachment_recheck_pending"):
            # 冲突补查后直接复判：冲突提示不能被「证据不足」盖掉。
            return "diagnose"
        # 第二轮容器可能已运行；仍需把失败业务探针交给诊断，再校验根因是否支持动作。
        collected = [Evidence.model_validate(e) for e in state["evidence"]]
        scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
        # 观测还不完整时，只要该场景的故障已经被探针证实，就继续诊断，不在采证处停住。
        if observations_available(collected, scenario) or remaining_fault(collected, scenario):
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
                if not state.get("attachment_recheck_count"):
                    # 第一次发现冲突：先重新采一轮证据再判（§5.3 情形 3「提示冲突并补查」）。
                    self.phase(
                        state,
                        Status.DIAGNOSING,
                        attachment_conflict=conflict,
                        attachment_recheck_count=1,
                    )
                    return {
                        "attachment_recheck_count": 1,
                        "attachment_recheck_pending": True,
                    }
                collected = [Evidence.model_validate(item) for item in state["evidence"]]
                scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
                if not remaining_fault(collected, scenario):
                    raise DomainError("EVIDENCE_CONFLICT", conflict)
            collected = [Evidence.model_validate(e) for e in state["evidence"]]
            scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
            context = {
                "incident_id": state["incident_id"],
                "scenario_key": state.get("scenario_key", "MYSQL_DOWN"),
                "attempt": state["attempt"],
                "evidence": state["evidence"],
                "previous_verification": state.get("verification"),
                "previous_execution": state.get("execution"),
            }
            feedback = None
            last_error: DomainError | None = None
            for attempt in range(1, DIAGNOSIS_ATTEMPTS + 1):
                trace = ModelTrace()
                if feedback:
                    context = {**context, "repair_note": feedback}
                try:
                    result = self.diagnosis.diagnose(context, trace=trace)
                except DomainError as error:
                    last_error = error
                    feedback = repair_feedback(error)
                    continue
                try:
                    validate_evidence_integrity(result, collected)
                except DomainError as error:
                    last_error = error
                    feedback = repair_feedback(error)
                    continue
                # 语义校验拦截前保存模型结论，UNKNOWN 也必须能回溯其依据与疑点。
                payload = result.model_dump(mode="json")
                self.phase(
                    state,
                    Status.DIAGNOSING,
                    diagnosis=payload,
                    diagnosis_source="model",
                    model_attempts=attempt,
                    attachment_recheck_pending=False,
                    repair_note=None,
                    # 模型原始输出单独留痕：审计要的是模型真的说了什么，不是我们解析后的结论。
                    raw_model_output=trace.raw.get("analysis"),
                    model_usage=trace.usage,
                )
                return {
                    "diagnosis": payload,
                    "model_attempts": attempt,
                    "attachment_recheck_pending": False,
                }
            fallback = registered_diagnosis(collected, scenario)
            if fallback is None or last_error is None:
                raise last_error or DomainError("AI_UNAVAILABLE", "诊断没有可用结论", 503)
            payload = fallback.model_dump(mode="json")
            note = "模型没有给出可用结论。已按探针和该场景注册的恢复动作继续，批准后才会执行。"
            self.phase(
                state,
                Status.DIAGNOSING,
                diagnosis=payload,
                diagnosis_source="registered_repair",
                model_attempts=DIAGNOSIS_ATTEMPTS,
                model_error={"code": last_error.code, "message": last_error.message},
                repair_note=note,
                attachment_recheck_pending=False,
            )
            return {
                "diagnosis": payload,
                "model_attempts": DIAGNOSIS_ATTEMPTS,
                "attachment_recheck_pending": False,
            }

        return self.guarded(state, Status.DIAGNOSING, operation)

    # ---- M3：受限主动补采 ---------------------------------------------------

    def after_authorize(self, state):
        if state.get("error"):
            return END
        if state.get("topology_recheck_pending"):
            return "supplement"
        return "plan"

    def supplement(self, state):
        """按模型请求补采观测。

        只能补采注册过的检查 ID，且不重复采集已有证据；
        轮次与总耗时都由配置封顶，超限就不再补采并如实记录原因。
        拓扑解释与模型矛盾时，也走这一次补采：复测规则点名的探针，然后回到诊断。
        """

        def operation():
            if state.get("topology_recheck_pending"):
                requested = [
                    {"check_id": check_id, "reason": "拓扑解释与模型根因矛盾，复测这条探针"}
                    for check_id in state.get("topology_recheck_checks") or []
                    if check_id in CHECK_IDS
                ][: self.settings.supplement_max_checks]
            else:
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
                "topology_recheck_pending": False,
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
        if state.get("attachment_recheck_pending"):
            # 截图与实测冲突：补采一轮再判一次，仍冲突才收尾。
            return "collect"
        requested = (state.get("diagnosis") or {}).get("requested_checks") or []
        if not requested:
            return "authorize"
        rounds = state.get("supplement_count", 0)
        if rounds >= self.settings.supplement_max_rounds:
            self.phase(state, Status.DIAGNOSING, supplement_stopped="已达补采轮次上限")
            return "authorize"
        if self.elapsed_seconds(state) >= self.settings.supplement_budget_seconds:
            self.phase(state, Status.DIAGNOSING, supplement_stopped="补采耗时预算已用尽")
            return "authorize"
        if not self.pending_requests(state):
            self.phase(state, Status.DIAGNOSING, supplement_stopped="请求的检查已全部采集或未注册")
            return "authorize"
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

    def accept_plan(self, state, result: Plan, source: str, attempts: int) -> dict:
        scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
        plan_id = new_id("plan")
        payload = result.model_dump(mode="json")
        # 先留痕再校验：被白名单门禁拦下的非法提议也必须计数。
        with self.store.session() as session, session.begin():
            self.store.audit(
                session,
                state["incident_id"],
                "plan_proposal",
                {"plan_id": plan_id, "attempt": state["attempt"], "source": source, **payload},
            )
        validate_plan(result, scenario)
        self.phase(
            state,
            Status.WAITING_APPROVAL,
            plan=payload,
            plan_id=plan_id,
            plan_source=source,
            plan_canonical=result.action_id == SCENARIOS[scenario].action,
            model_attempts=attempts,
        )
        with self.store.session() as session, session.begin():
            self.store.audit(
                session,
                state["incident_id"],
                "plan",
                {"plan_id": plan_id, "attempt": state["attempt"], "source": source, **payload},
            )
        return {"plan": payload, "plan_id": plan_id, "model_attempts": attempts}

    def plan(self, state):
        def operation():
            scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
            collected = [Evidence.model_validate(item) for item in state["evidence"]]
            # 截图不能决定恢复。探针没有证实故障时才停；故障已证实就继续修。
            conflict = self.attachment_conflict(state)
            if conflict and not remaining_fault(collected, scenario):
                raise DomainError("EVIDENCE_CONFLICT", conflict)
            context = {
                "diagnosis": state["diagnosis"],
                "scenario_key": state.get("scenario_key", "MYSQL_DOWN"),
                "evidence": state["evidence"],
                "previous_verification": state.get("verification"),
                "previous_execution": state.get("execution"),
            }
            feedback = None
            last_error: DomainError | None = None
            for attempt in range(1, PLAN_ATTEMPTS + 1):
                if feedback:
                    context = {**context, "repair_note": feedback}
                try:
                    result = self.planner.plan(context)
                except DomainError as error:
                    last_error = error
                    feedback = repair_feedback(error)
                    continue
                try:
                    return self.accept_plan(state, result, "model", attempt)
                except DomainError as error:
                    last_error = error
                    feedback = repair_feedback(error)
            fallback = (
                registered_plan(scenario, collected)
                if probes_support_registered_fix(collected, scenario)
                else None
            )
            if fallback is None or last_error is None:
                raise last_error or DomainError("AI_UNAVAILABLE", "规划没有可用结论", 503)
            note = "模型给出的计划不能执行。已改为该场景已注册的恢复动作，批准后才会执行。"
            accepted = self.accept_plan(state, fallback, "registered_repair", PLAN_ATTEMPTS)
            self.phase(
                state,
                Status.WAITING_APPROVAL,
                repair_note=note,
                plan_source="registered_repair",
            )
            return accepted

        return self.guarded(state, Status.PLANNING, operation)

    def continue_repair(self, state, explanation: dict, rule_ms: int) -> dict:
        note = "模型结论不能直接执行。探针已证实故障，改为该场景已注册的恢复动作，批准后才会执行。"
        self.phase(state, Status.DIAGNOSING, repair_note=note, rule_ms=rule_ms)
        return {
            "topology_explanation": explanation,
            "rule_ms": rule_ms,
            "topology_recheck_pending": False,
        }

    def authorize(self, state):
        """执行许可。模型与规则矛盾时先补采一次。

        补采后仍矛盾，或模型结论不足以执行时，只要探针已经证实该场景的故障，
        就不在这里停住，改由注册恢复动作继续。探针没证实故障时才结束。
        规则解释单独计时，不记成模型调用。
        """

        def operation():
            collected = [Evidence.model_validate(e) for e in state["evidence"]]
            scenario = Scenario(state.get("scenario_key", "MYSQL_DOWN"))
            explanation = explain_from_evidence(collected)
            rule_ms = int(explanation.get("rule_ms") or 0)
            diagnosis = Diagnosis.model_validate(state["diagnosis"])
            confirmed = remaining_fault(collected, scenario)
            self.phase(
                state,
                Status.DIAGNOSING,
                topology_explanation=explanation,
                rule_ms=rule_ms,
            )
            if contradicts(diagnosis.root_cause, explanation):
                already = (
                    state.get("supplement_count", 0) >= 1
                    or state.get("topology_recheck_count", 0) >= 1
                )
                checks = [
                    check_id
                    for check_id in explanation.get("cited_checks") or []
                    if check_id in CHECK_IDS
                ][: self.settings.supplement_max_checks]
                if not already and checks:
                    return {
                        "topology_recheck_pending": True,
                        "topology_recheck_count": state.get("topology_recheck_count", 0) + 1,
                        "topology_recheck_checks": checks,
                        "topology_explanation": explanation,
                        "rule_ms": rule_ms,
                    }
                if confirmed:
                    return self.continue_repair(state, explanation, rule_ms)
                raise DomainError(
                    "EVIDENCE_CONFLICT",
                    explanation.get("reason") or "模型根因与拓扑解释矛盾",
                    details={"topology_explanation": explanation, "rule_ms": rule_ms},
                )
            try:
                validate_execution_preconditions(diagnosis, collected, scenario)
            except DomainError:
                if confirmed:
                    return self.continue_repair(state, explanation, rule_ms)
                raise
            return {
                "topology_explanation": explanation,
                "rule_ms": rule_ms,
                "topology_recheck_pending": False,
            }

        return self.guarded(state, Status.DIAGNOSING, operation, label="AUTHORIZING")

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
