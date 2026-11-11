from sqlalchemy import literal_column, select

from app.config import DomainError
from app.repositories.store import Record, Store
from app.scenarios import SCENARIOS
from app.schemas.domain import Action, Plan, Scenario, Status


def plan_violations(plan: Plan | dict, scenario: Scenario) -> list[str]:
    """列出计划与场景注册表不符的地方；空列表表示合法。

    评测指标要用它统计「错误处置提议数」（§9.1），
    因此判定口径必须与门禁本身同源，不能另写一份。
    """
    definition = SCENARIOS[scenario]
    data = plan if isinstance(plan, dict) else plan.model_dump(mode="json")
    action = data.get("action_id")
    expected_target = "none" if action == Action.NO_ACTION else definition.target
    violations: list[str] = []
    if action not in {definition.action, Action.NO_ACTION}:
        violations.append(f"动作 {action} 不在场景白名单内")
    if data.get("target") != expected_target:
        violations.append(f"目标 {data.get('target')} 与场景不符")
    expected = sorted(definition.checks)
    if sorted(data.get("expected_checks") or []) != expected:
        violations.append("探针集合与场景不符")
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
            elif plan.action_id == Action.RESTORE_NGINX_UPSTREAM:
                output = self.docker.apply_nginx_upstream("healthy")
            elif plan.action_id == Action.RESTORE_APP_MODE:
                output = self.docker.apply_app_mode("ok")
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
