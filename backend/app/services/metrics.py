"""端到端对照指标（docs/落地执行计划.md §9.1）。

纯诊断路径永远拿不到执行器入口、也不调用 Planner，因此**根本不产生处置提议**；
错误处置提议数与拦截率只在端到端（闭环）路径上统计，纯诊断页面显示「不适用」而不是 0。

这里的数据全部来自工单已保存的审计记录，不现场重算。
"""

from sqlalchemy import select

from app.repositories.store import Incident, Record, Store
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
                # 执行许可门禁不是模型调用：不能计入调用次数。
                if (
                    phase in {"DIAGNOSING", "PLANNING"}
                    and payload.get("step_kind") != "AUTHORIZING"
                ):
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
        "model_calls_avg": round(model_calls / len(incidents), 2) if incidents else None,
        "model_calls_unknown": 0,
        "note": "端到端指标只统计闭环路径真实产生的处置提议；纯诊断对照不适用。",
    }
