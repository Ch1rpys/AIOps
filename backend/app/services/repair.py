"""模型不可用或结论不能执行时，改走探针已经证实的注册恢复动作。

只用于闭环。盲诊评测不调用这里，避免把规则补救记成模型答对。
探针没有证实故障时，不编造修复。
"""

from app.agents.evidence import remaining_fault, service_fault
from app.config import DomainError
from app.scenarios import SCENARIOS
from app.schemas.domain import Action, Diagnosis, Evidence, Plan, Scenario, required_evidence_ids

DIAGNOSIS_ATTEMPTS = 3
PLAN_ATTEMPTS = 2
DIAGNOSIS_CAUSES = {
    "MYSQL_SERVICE_STOPPED",
    "REDIS_SERVICE_STOPPED",
    "UPSTREAM_APP_STOPPED",
    "NGINX_UPSTREAM_MISCONFIGURED",
    "INSUFFICIENT_EVIDENCE",
    "OTHER",
}


def repair_feedback(error: DomainError) -> str:
    return (
        f"上一次输出不能用（{error.code}：{error.message}）。"
        "请只返回一个 JSON 对象，并给出与实测证据一致、可以执行的结论。"
    )


def probes_support_registered_fix(
    evidence: list[Evidence], scenario: Scenario, explanation: dict | None = None
) -> bool:
    """该场景的故障已经被探针证实，并且存在一个会动手的注册动作。"""
    del explanation
    definition = SCENARIOS[scenario]
    if definition.action == Action.NO_ACTION:
        return False
    return remaining_fault(evidence, scenario)


def registered_diagnosis(evidence: list[Evidence], scenario: Scenario) -> Diagnosis | None:
    if not probes_support_registered_fix(evidence, scenario):
        return None
    evidence_ids = required_evidence_ids(evidence)
    if not evidence_ids:
        return None
    definition = SCENARIOS[scenario]
    cause = definition.root_cause if definition.root_cause in DIAGNOSIS_CAUSES else "OTHER"
    return Diagnosis(
        root_cause=cause,
        summary=f"模型没有给出可用结论。探针确认{definition.name}，继续使用已注册的恢复动作。",
        evidence_ids=evidence_ids[:30],
        confidence=0.0,
        evidence_sufficient=True,
    )


def registered_plan(scenario: Scenario, evidence: list[Evidence] | None = None) -> Plan | None:
    definition = SCENARIOS[scenario]
    if definition.action == Action.NO_ACTION:
        return None
    action_id = definition.action
    target = definition.target
    if definition.inject == "stop_pair" and evidence is not None:
        if service_fault(evidence, "demo-mysql", ("mysql_select_1", "app_db_check")):
            action_id, target = Action.START_DEMO_MYSQL, "demo-mysql"
        elif service_fault(evidence, "demo-redis", ("redis_roundtrip", "app_cache_check")):
            action_id, target = Action.START_DEMO_REDIS, "demo-redis"
        else:
            return None
    return Plan(
        action_id=action_id,
        target=target,
        reason="模型给出的计划不能执行。探针仍显示故障，改为该场景已注册的恢复动作，批准后才会执行。",
        risk="LOW",
        expected_checks=list(definition.checks),
    )
