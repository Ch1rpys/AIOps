import logging
import threading
import time

import httpx
from langgraph.types import Command
from sqlalchemy import select

from app.agents.evidence import fault_observed
from app.config import DomainError
from app.repositories.store import Incident, LabLease, RequestReceipt
from app.schemas.domain import TERMINAL, Scenario, Status, new_id, now
from app.services.compiler.canonical import canonical_recipe
from app.services.compiler.apply import apply_recipe, undo_recipe
from app.services.compiler.schema import Recipe
from app.services.compiler.split import is_holdout
from app.services.faults import apply_fault, clear_fault, clear_lab
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
    def __init__(
        self,
        settings,
        store,
        docker,
        probes,
        evidence,
        workflow,
        client,
        mode="REAL",
        attachments=None,
        twin=None,
    ):
        self.settings, self.store, self.docker = settings, store, docker
        self.probes, self.evidence, self.workflow = probes, evidence, workflow
        self.client, self.mode = client, mode
        self.attachments = attachments
        self.twin = twin
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
        # 进程崩在评测中途时，评测租约会留在库里并锁死工作台；
        # 启动时清掉过期租约，不要求操作者手改数据库。
        if self.store.clear_expired_leases():
            logger.warning("stale_evaluation_lease_cleared")

    @staticmethod
    def graph_config(incident_id: str) -> dict:
        return {"configurable": {"thread_id": incident_id}, "recursion_limit": 30}

    def environment(self) -> dict:
        baseline = lab_baseline(self.docker, self.probes)
        with self.store.session() as session:
            lease = session.get(LabLease, 1)
            owner = lease.incident_id if lease else None
        twin_status = None
        if self.twin is not None:
            twin_status = self.twin.status()
        else:
            twin_status = {
                "status": "UNKNOWN",
                "healthy": False,
                "world": "twin",
                "compose_project": "aiops-twin",
                "services": {},
                "probes": [],
                "error": {"code": "TWIN_UNAVAILABLE", "message": "影子世界未配置"},
                "locked": False,
            }
        return {
            **baseline,
            "active_incident_id": owner,
            "captured_at": now(),
            "twin": twin_status,
        }

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
        vision_configured = bool(
            self.attachments is not None and self.attachments.provider.configured()
        )
        lab = self.environment()
        return {
            "platform": "PASS",
            "mode": self.mode,
            "holmes": holmes,
            "planner": {
                "status": "UNKNOWN",
                "configured": configured,
                "reason": "须由真实模型调用验证连通性",
            },
            # 空值不伪装成已配置：没配视觉模型就如实显示 UNKNOWN。
            "vision": {
                "status": "UNKNOWN",
                "configured": vision_configured,
                "reason": (
                    "视觉模型未配置，视觉证据不可用"
                    if not vision_configured
                    else "须由真实模型调用验证连通性"
                ),
            },
            "admin_configured": bool(self.settings.admin_token.get_secret_value()),
            "lab": lab,
            "twin": lab.get("twin") or {},
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

    def inject(self, scenario: Scenario, key: str, poison: bool = False) -> dict:
        scope = "inject"
        fingerprint = f"{scenario.value}:poison" if poison else scenario.value
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
                        "poison": poison,
                        **self._recipe_detail(canonical_recipe(scenario)),
                    },
                )
                session.add(item)
                session.flush()
                session.add(LabLease(id=1, incident_id=incident_id))
                self.store.save_receipt(session, key, scope, fingerprint, {"pending": True})
            try:
                output = apply_fault(self.docker, scenario)
                if self.twin is not None and self.twin.available():
                    try:
                        apply_fault(self.twin.docker, scenario)
                    except Exception:
                        clear_fault(self.docker, scenario)
                        raise DomainError("TWIN_UNAVAILABLE", "影子注入失败，已回滚真环境", 503) from None
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
                outputs = clear_lab(self.docker)
                if self.twin is not None:
                    try:
                        outputs.extend(clear_lab(self.twin.docker))
                    except DomainError:
                        pass
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

    @staticmethod
    def _recipe_detail(recipe: Recipe) -> dict:
        signature = recipe.signature.model_dump(mode="json") if recipe.signature else None
        return {
            "recipe_id": recipe.id,
            "recipe_title": recipe.title,
            "signature": signature,
            "recipe": recipe.model_dump(mode="json"),
        }

    def worlds(self) -> dict:
        env = self.environment()
        return {
            "primary": {
                "name": "primary",
                "compose_project": env.get("compose_project"),
                "healthy": env.get("healthy"),
                "services": env.get("services"),
                "probes": env.get("probes"),
                "error": env.get("error"),
            },
            "twin": env.get("twin") or {},
        }

    def service_logs(self, service: str, world: str = "primary") -> dict:
        if world == "twin":
            if self.twin is None:
                raise DomainError("TWIN_UNAVAILABLE", "影子世界未配置", 503)
            docker = self.twin.docker
        else:
            docker = self.docker
        return {"world": world, "service": service, "text": docker.logs(service)}

    def pick_seen_recipe(self) -> Recipe:
        from app.services.compiler.enumerate import eligible_pool
        from app.services.compiler.split import calibrated_eligible, freeze_ids

        holdout = set(freeze_ids())
        pool = [
            recipe
            for recipe in calibrated_eligible()
            if recipe.id not in holdout and not recipe.poison
        ]
        if not pool:
            pool = [
                recipe
                for recipe in eligible_pool()
                if recipe.id not in holdout and not recipe.poison
            ]
        if not pool:
            raise DomainError("RECIPE_INVALID", "没有可用的 seen 配方", 409)
        index = int(time.time() * 1000) % len(pool)
        recipe = pool[index]
        stored = self.store.get_recipe(recipe.id)
        return stored or recipe

    def load_recipe(self, recipe_id: str, *, for_lab: bool) -> Recipe:
        stored = self.store.get_recipe(recipe_id)
        if stored is None:
            from app.services.compiler.enumerate import enumerate_recipes

            match = next((item for item in enumerate_recipes() if item.id == recipe_id), None)
            if match is None:
                raise DomainError("RECIPE_INVALID", "配方不存在", 404)
            stored = match
        if for_lab and (stored.holdout or is_holdout(stored.id)):
            raise DomainError("HOLDOUT_FORBIDDEN_IN_LAB", "这是评测考题，禁止在工作台注入", 409)
        if not for_lab and not (stored.holdout or is_holdout(stored.id)):
            raise DomainError("SEEN_FORBIDDEN_IN_UNSEEN", "未见评测只能抽 holdout", 409)
        return stored

    def inject_recipe(self, recipe: Recipe, key: str, poison: bool = False) -> dict:
        if is_holdout(recipe.id) or recipe.holdout:
            raise DomainError("HOLDOUT_FORBIDDEN_IN_LAB", "这是评测考题，禁止在工作台注入", 409)
        if self.twin is None or not self.twin.available():
            raise DomainError("TWIN_UNAVAILABLE", "影子世界不可用，无法双侧注入", 503)
        scope = "compile-inject"
        fingerprint = recipe.id + (":poison" if poison else "")
        with self.lock:
            with self.store.session() as session:
                previous = self.replay(self.store.receipt(session, key, scope, fingerprint))
                if previous:
                    return previous
                if session.get(LabLease, 1):
                    raise DomainError("LAB_BUSY", "实验环境已被占用，请先完成或重置当前实验")
            if self.store.evaluation_busy():
                raise DomainError("EVALUATION_BUSY", "评测进行中，请等待评测结束后再注入故障", 409)
            baseline = self.environment()
            if not baseline["healthy"]:
                raise DomainError("BASELINE_UNHEALTHY", "四个 demo 服务与探针未全部通过，禁止注入")
            from app.schemas.domain import Scenario as ScenarioEnum

            scene = (
                recipe.canonical_scene.value
                if recipe.canonical_scene
                else "COMPILED"
            )
            if scene not in {item.value for item in ScenarioEnum}:
                scene = "COMPILED"
            incident_id = new_id("inc")
            with self.store.session() as session, session.begin():
                item = Incident(
                    id=incident_id,
                    scenario_key=scene,
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
                        "poison": poison or recipe.poison,
                        **self._recipe_detail(recipe),
                    },
                )
                session.add(item)
                session.flush()
                session.add(LabLease(id=1, incident_id=incident_id))
                self.store.save_receipt(session, key, scope, fingerprint, {"pending": True})
            try:
                output = apply_recipe(self.docker, recipe)
                try:
                    apply_recipe(self.twin.docker, recipe)
                except Exception:
                    undo_recipe(self.docker, recipe)
                    raise DomainError("TWIN_UNAVAILABLE", "影子注入失败，已回滚真环境", 503) from None
                evidence = []
                confirmed = False
                for index in range(self.settings.fault_confirmation_attempts):
                    if index:
                        time.sleep(self.settings.fault_confirmation_interval)
                    sample = self.evidence.collect_domain()
                    evidence.extend(sample)
                    from app.agents.evidence import _snapshot_and_probes
                    from app.schemas.domain import Verdict

                    _, probes = _snapshot_and_probes(sample)
                    failing = {probe.name for probe in probes if probe.verdict == Verdict.FAIL}
                    expected = set(
                        (recipe.signature.failing_checks if recipe.signature else []) or []
                    )
                    confirmed = expected <= failing if expected else bool(failing)
                    if confirmed:
                        break
                with self.store.session() as session, session.begin():
                    item = self.store.require(session, incident_id)
                    self.store.audit(session, incident_id, "recipe_inject", output)
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
                    undo_recipe(self.docker, recipe)
                    undo_recipe(self.twin.docker, recipe)
                    raise DomainError(
                        "FAULT_NOT_CONFIRMED", "注入后未取得配方对应的故障证据", 502
                    )
                response = {
                    "incident_id": incident_id,
                    "scenario_key": scene,
                    "status": Status.INJECTED,
                    "fault_confirmed": True,
                    "recipe_id": recipe.id,
                    "recipe_title": recipe.title,
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
