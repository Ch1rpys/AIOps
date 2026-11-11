"""端到端对照指标（docs/落地执行计划.md §9.1）。

纯诊断路径永远拿不到执行器入口、也不调用 Planner，因此**根本不产生处置提议**；
错误处置提议数与拦截率只在端到端（闭环）路径上统计，纯诊断页面显示「不适用」而不是 0。

这里的数据全部来自工单已保存的审计记录，不现场重算。
"""

from sqlalchemy import select

from app.repositories.store import EvaluationRun, Incident, Record, Store
from app.schemas.domain import Scenario
from app.services.executor import plan_violations


def end_to_end_summary(store: Store) -> dict:
    """闭环路径的指标：错误处置提议数、拦截率、恢复成功率、模型调用次数。"""
    with store.session() as session:
        incidents = session.scalars(select(Incident)).all()
        records = session.scalars(select(Record)).all()
    by_incident: dict[str, list[Record]] = {}
    for record in records:
        by_incident.setdefault(record.incident_id, []).append(record)

    proposals = 0
    illegal = 0
    blocked = 0
    executed = 0
    recovered = 0
    model_calls = 0
    for incident in incidents:
        rows = by_incident.get(incident.id, [])
        validated = {row.payload.get("plan_id") for row in rows if row.kind == "plan"}
        for row in rows:
            if row.kind == "plan_proposal":
                proposals += 1
                try:
                    scenario = Scenario(incident.scenario_key)
                except ValueError:
                    continue
                if plan_violations(row.payload, scenario):
                    illegal += 1
                    if row.payload.get("plan_id") not in validated:
                        # 没有对应的已校验计划记录 = 被白名单门禁拦下。
                        blocked += 1
            if row.kind == "step":
                payload = row.payload or {}
                phase = payload.get("phase")
                # 只有真的产出了模型结论的步骤才算一次模型调用：
                # 执行许可门禁、截图冲突后的补查都没有调模型。
                output = payload.get("output") or {}
                if phase == "DIAGNOSING" and output.get("diagnosis"):
                    model_calls += 1
                elif phase == "PLANNING" and output.get("plan"):
                    model_calls += 1
        if any(row.kind == "execution" for row in rows):
            executed += 1
            verification = (incident.detail or {}).get("verification") or {}
            if verification.get("verdict") == "PASS":
                recovered += 1
    return {
        "applicable": True,
        "incidents": len(incidents),
        "proposals": proposals,
        "illegal_proposals": illegal,
        "illegal_proposal_rate": round(illegal / proposals, 4) if proposals else None,
        "blocked_proposals": blocked,
        "block_rate": round(blocked / illegal, 4) if illegal else None,
        "executed": executed,
        "recovery_success_rate": round(recovered / executed, 4) if executed else None,
        "model_calls": model_calls,
        # 平均每次工单花了几次模型调用；没有工单时是「不适用」。
        "model_calls_avg": round(model_calls / len(incidents), 2) if incidents else None,
        # 闭环路径的调用次数来自审计记录本身，不存在「未知」这一说。
        "model_calls_unknown": 0,
        "note": "端到端指标只统计闭环路径真实产生的处置提议；纯诊断对照不适用。",
    }


def adversarial_closed_loop(store: Store) -> dict:
    """对抗样本里闭环臂的处置提议。

    这些提议走的是同一套 plan_violations，但没有工单。
    没跑闭环臂时返回「未测量」，调用方不得把它显示成 0。
    """
    with store.session() as session:
        items = session.scalars(
            select(EvaluationRun).where(EvaluationRun.cohort == "adversarial")
        ).all()
    proposals = illegal = 0
    executed = 0
    measured = False
    for item in items:
        if item.arm != "L":
            continue
        diagnosis = item.diagnosis_json or {}
        adversarial = diagnosis.get("adversarial") or {}
        if adversarial.get("illegal_action_proposed") is None or not diagnosis.get("plan"):
            continue
        measured = True
        proposals += 1
        if adversarial.get("illegal_action_proposed"):
            illegal += 1
        executed += int(adversarial.get("illegal_action_executed") or 0)
    return {
        "measured": measured,
        "proposals": proposals,
        "illegal_proposals": illegal,
        "blocked_proposals": illegal,
        "illegal_action_executed": executed if measured else None,
    }
