from sqlalchemy import literal_column, select

from app.config import DomainError
from app.repositories.store import Record, Store
from app.scenarios import SCENARIOS
from app.schemas.domain import Action, Plan, Scenario, Status


def validate_plan(plan: Plan, scenario: Scenario) -> None:
    definition = SCENARIOS[scenario]
    expected_target = "none" if plan.action_id == Action.NO_ACTION else definition.target
    if (
        plan.action_id not in {definition.action, Action.NO_ACTION}
        or plan.target != expected_target
        or len(plan.expected_checks) != len(definition.checks)
        or set(plan.expected_checks) != set(definition.checks)
    ):
        raise DomainError("BLOCKED", "动作、场景、目标或探针不匹配")


class ActionExecutor:
    def __init__(self, store: Store, docker):
        self.store, self.docker = store, docker

    def execute(self, incident_id: str, plan_id: str, plan: Plan) -> dict:
        with self.store.session() as session, session.begin():
            item = self.store.require(session, incident_id)
            validate_plan(plan, Scenario(item.scenario_key))
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
            if plan.action_id == Action.NO_ACTION:
                output = {"operation": "NO_ACTION", "exit_code": 0}
            else:
                output = self.docker.change(plan.target, "start")
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
