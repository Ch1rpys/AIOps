from sqlalchemy import literal_column, select

from app.config import CHECK_IDS, DomainError
from app.repositories.store import Record, Store
from app.schemas.domain import Plan, Scenario, Status
from app.services.action_catalog import lookup, perform


def plan_violations(plan: Plan | dict, scenario: Scenario) -> list[str]:
    """目录之外的动作才算非法。目录里的其他路线由复测判断对错。

    评测指标与这道门禁同源。场景名只保留参数，避免另写一套判定。
    """
    del scenario
    data = plan if isinstance(plan, dict) else plan.model_dump(mode="json")
    action = str(data.get("action_id") or "")
    entry = lookup(action)
    violations: list[str] = []
    if entry is None:
        violations.append(f"动作 {action} 不在修复目录中")
        return violations
    if data.get("target") != entry.target:
        violations.append(f"目标 {data.get('target')} 与该目录动作不符")
    checks = data.get("expected_checks") or []
    if not checks or any(check not in CHECK_IDS for check in checks):
        violations.append("探针不在已登记检查中")
    return violations


def validate_plan(plan: Plan, scenario: Scenario) -> None:
    if plan_violations(plan, scenario):
        raise DomainError("BLOCKED", "动作、场景、目标或探针不匹配")


class ActionExecutor:
    def __init__(self, store: Store, docker):
        self.store, self.docker = store, docker

    def execute(self, incident_id: str, plan_id: str, plan: Plan) -> dict:
        with self.store.session() as session, session.begin():
            item = self.store.require(session, incident_id)
            try:
                scenario = Scenario(item.scenario_key)
            except ValueError:
                scenario = Scenario.MYSQL_DOWN
            validate_plan(plan, scenario)
            approvals = session.scalars(
                select(Record).where(Record.incident_id == incident_id, Record.kind == "approval")
            ).all()
            if not any(
                a.payload.get("plan_id") == plan_id and a.payload.get("decision") == "approve"
                for a in approvals
            ):
                raise DomainError("BLOCKED", "没有该计划的有效人工批准")
            if item.detail.get("plan_id") != plan_id or item.status != Status.EXECUTING:
                raise DomainError("BLOCKED", "计划已过期或状态不允许执行")
            executions = session.scalars(
                select(Record)
                .where(Record.incident_id == incident_id, Record.kind == "execution")
                .order_by(literal_column("rowid"))
            ).all()
            matching = [r.payload for r in executions if r.payload["plan_id"] == plan_id]
            if matching:
                previous = matching[-1]
                if previous["state"] == "SUCCEEDED":
                    return previous
                # 外部写入与本地事务无法原子提交；不确定是否执行时禁止自动重放。
                raise DomainError("EXECUTION_UNCERTAIN", "动作已尝试，需检查环境后重置")
            successful = [
                r.payload
                for r in executions
                if r.payload.get("state") == "SUCCEEDED"
                and r.payload.get("action_id") == plan.action_id
            ]
            if successful:
                result = {**successful[-1], "plan_id": plan_id, "reused": True}
                self.store.audit(session, incident_id, "execution", result)
                return result
            self.store.audit(
                session,
                incident_id,
                "execution",
                {"plan_id": plan_id, "action_id": plan.action_id, "state": "STARTED"},
            )
        try:
            output = perform(self.docker, plan.action_id)
        except DomainError as error:
            with self.store.session() as session, session.begin():
                self.store.audit(
                    session,
                    incident_id,
                    "execution",
                    {
                        "plan_id": plan_id,
                        "action_id": plan.action_id,
                        "state": "FAILED" if error.details else "UNKNOWN",
                        "error": {"code": error.code, "message": error.message},
                        "output": error.details or {"exit_code": None},
                    },
                )
            raise
        result = {
            "plan_id": plan_id,
            "action_id": plan.action_id,
            "state": "SUCCEEDED",
            "output": output,
        }
        with self.store.session() as session, session.begin():
            self.store.audit(session, incident_id, "execution", result)
        return result
