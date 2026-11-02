"""盲诊评测服务。

见 docs/落地执行计划.md §4.4 / §4.5 / §9。
与工作台闭环**分开建链**，只复用底层组件（EvidenceAgent / Probes / DockerController / Store）：

- 不创建 Incident 行：真值只落在 ``evaluation_runs.truth_scenario`` 一处，
  不会借道 ``GET /api/incidents`` 泄露到工作台；
- 不调用 Planner、不调用 ActionExecutor：盲诊路径**永远拿不到执行器入口**；
- 只做引用完整性校验，不做执行许可校验：答错与拒答都要能落库，
  否则正确率无从统计。

租约策略（二选一，见 §4.4）：采用**独立评测租约**。
评测与工作台的 lab_lease 互斥；工作台注入在评测进行中会被明确拒绝。
"""

import hashlib
import json
import logging
import threading
import time

from app.agents.evidence import EvidenceAgent, fault_observed, validate_evidence_integrity
from app.config import DomainError, Settings
from app.integrations.docker import DockerController
from app.integrations.models import ModelTrace
from app.integrations.probes import Probes
from app.repositories.store import EvaluationArm, EvaluationRun, Store
from app.scenarios import SCENARIOS
from app.schemas.domain import Evidence, Scenario, new_id, now, required_evidence_ids
from app.services.lab import lab_baseline

logger = logging.getLogger("aiops.evaluation")

PROMPT_VERSION = "blind-v1"
# 不计入正确率分母的状态：超限与基础设施故障不是模型能力问题，
# 混进同一个分母会污染正确率（见 §9.4）。
EXCLUDED_STATUS = {"BUDGET_EXCEEDED", "ERROR"}


def merge_usage(first: dict | None, second: dict | None) -> tuple[dict | None, bool]:
    """合并两次模型调用的 token 用量，返回（用量，是否只是部分已知）。

    只有一份已知、或两份的字段集合不一致时，不能当成总量 ——
    汇总里要把这种用量排除在总量之外，否则会把「少算」显示成「全量」。
    """
    if first is None and second is None:
        return None, False
    fields_first = set(first or {})
    fields_second = set(second or {})
    merged: dict[str, int] = {}
    for usage in (first or {}, second or {}):
        for key, value in usage.items():
            if isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
    partial = bool(first) != bool(second) or fields_first != fields_second
    return (merged or None), partial


class Budget:
    """§9.4 的预算口径：预算是**上限**不是配额。

    超限时终止本次实验并记录（记为该臂的一个观测样本），不是静默截断。

    预算覆盖范围（实现前定死，见 docs/落地执行计划.md §14.1）：
    **每个实验臂各自一份**，覆盖该臂自己的模型调用与它触发的额外补采。
    两臂共用的证据快照采集不计入臂预算 —— 两次采集完全相同，
    摊到臂上会平白惩罚后跑的那一臂，也反映不出两臂的相对代价；
    这段固定开销单独作为「采证时间」显示（§9.2）。
    """

    def __init__(self, seconds: int, max_calls: int):
        self.limit_ms = seconds * 1000
        self.max_calls = max_calls
        self.started = time.monotonic()
        self.calls = 0

    @property
    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise DomainError("EVALUATION_BUDGET_EXCEEDED", "模型调用次数超出评测预算上限", 409)
        if self.elapsed_ms >= self.limit_ms:
            raise DomainError("EVALUATION_BUDGET_EXCEEDED", "评测总耗时超出预算上限", 409)

    def charge(self) -> None:
        self.calls += 1


class EvaluationService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        docker: DockerController,
        probes: Probes,
        evidence: EvidenceAgent,
        provider,
    ):
        self.settings, self.store = settings, store
        self.docker, self.probes = docker, probes
        self.evidence, self.provider = evidence, provider
        self.lock = threading.RLock()

    # ---- 对外入口 ------------------------------------------------------------

    def run_experiment(self, scenario: Scenario, arms: list[str]) -> dict:
        """一次盲诊实验：注入 → 固化快照 → 各臂在同一快照上对照。"""
        holder = new_id("eval")
        with self.lock:
            self.store.acquire_evaluation_lease(holder)
            try:
                return self._experiment(scenario, arms)
            except Exception as error:
                # 失败也要落库：不能因为实验失败就看不到这次尝试（§9.3）。
                self._record_failure(scenario, arms, error)
                raise
            finally:
                self.store.release_evaluation_lease(holder)

    def _record_failure(self, scenario: Scenario, arms: list[str], error: Exception) -> None:
        """实验在中途失败：每个请求的臂各留一条失败记录。"""
        code = error.code if isinstance(error, DomainError) else type(error).__name__
        message = error.message if isinstance(error, DomainError) else str(error)
        fingerprint = hashlib.sha256(f"failed:{scenario.value}:{code}".encode()).hexdigest()
        for arm in arms:
            run = EvaluationRun(
                id=new_id("evalrun"),
                arm=arm,
                truth_scenario=scenario.value,
                snapshot_hash=fingerprint,
                evidence_json={},
                diagnosis_json={
                    "status": "ERROR",
                    "code": code,
                    "message": message,
                    "model_calls": 0,
                    "extra_collects": 0,
                    # 没跑成就没有耗时：写 0 等于伪造一个「0 毫秒」的实测值（§9.3）。
                    "timing": None,
                },
                correct=None,
                rejected=0,
                # 故障是否成立尚未确认：不能写 0（那表示「确认过但不成立」）。
                fault_present=None,
                model_calls=None,
                latency_ms=None,
                token_usage=None,
                created_at=now(),
            )
            self.store.save_evaluation_run(
                run,
                EvaluationArm(
                    id=new_id("evalarm"),
                    snapshot_hash=fingerprint,
                    model=self.settings.holmes_model or "service_default",
                    prompt_version=PROMPT_VERSION,
                    max_tokens=None,
                    timeout_s=self.settings.evaluation_model_timeout,
                    arm=arm,
                ),
            )

    def busy(self) -> bool:
        return self.store.evaluation_busy()

    # ---- 实验主流程 ----------------------------------------------------------

    def _experiment(self, scenario: Scenario, arms: list[str]) -> dict:
        # 一次实验只核验一次版本与工具权限：每次模型调用前都核验会让单臂
        # 最坏耗时突破 45 秒总预算，「超限终止」就不成立了（§9.4）。
        self.provider.ensure_capabilities()
        baseline = lab_baseline(self.docker, self.probes)
        if not baseline["healthy"]:
            raise DomainError("BASELINE_UNHEALTHY", "四个 demo 服务与探针未全部通过，禁止评测", 409)
        stopped = False
        restored = False
        container_ms = collect_ms = restore_ms = 0
        try:
            container_started = time.monotonic()
            self.docker.change(SCENARIOS[scenario].target, "stop")
            stopped = True
            container_ms = round((time.monotonic() - container_started) * 1000)
            snapshot, extra_collects, collect_ms = self._capture_snapshot(scenario)
        finally:
            # 无论实验成功还是中途失败，都要把实验环境恢复健康，
            # 不能把停掉的容器留在那里让工作台无法使用。
            if stopped:
                try:
                    restored, restore_ms = self._restore(scenario)
                except DomainError as error:
                    # 恢复失败不能顶掉原始错误：原始错误才是这次实验的根因。
                    logger.error(
                        "evaluation_restore_failed scenario=%s code=%s",
                        scenario.value,
                        error.code,
                    )
                    restored = False
                if not restored:
                    logger.error("evaluation_lab_not_restored scenario=%s", scenario.value)
        fingerprint = self._fingerprint(snapshot)

        rows = []
        for arm in arms:
            rows.append(
                self._run_arm(
                    arm,
                    scenario,
                    snapshot,
                    fingerprint,
                    extra_collects,
                    {
                        "collect_ms": collect_ms,
                        "container_ms": container_ms,
                        "restore_ms": restore_ms,
                        "lab_restored": restored,
                    },
                )
            )
        return {"snapshot_hash": fingerprint, "runs": rows}

    def _capture_snapshot(self, scenario: Scenario) -> tuple[list[Evidence], int, int]:
        """按盲诊口径采集证据：四服务全采 + 通用 Runbook，直到故障成立。

        故障确认只看容器状态与探针观测，不使用场景专属 Runbook，
        否则「通用 Runbook 不泄漏」这条会被绕过。
        """
        attempts = min(
            self.settings.fault_confirmation_attempts,
            1 + self.settings.evaluation_max_extra_collects,
        )
        started = time.monotonic()
        collected = 0
        evidence: list[Evidence] = []
        for index in range(attempts):
            if index:
                time.sleep(self.settings.fault_confirmation_interval)
            evidence = self.evidence.collect_domain()
            collected += 1
            if fault_observed(evidence, scenario):
                break
        else:
            raise DomainError("FAULT_NOT_CONFIRMED", "停止操作后未取得故障成立的证据", 502)
        return evidence, max(0, collected - 1), round((time.monotonic() - started) * 1000)

    def _restore(self, scenario: Scenario) -> tuple[bool, int]:
        """把实验环境恢复健康。评测只布置与拆除环境，不执行模型提议的处置。"""
        started = time.monotonic()
        self.docker.change(SCENARIOS[scenario].target, "start")
        for index in range(self.settings.verification_attempts):
            if index:
                time.sleep(self.settings.verification_interval)
            if lab_baseline(self.docker, self.probes)["healthy"]:
                return True, round((time.monotonic() - started) * 1000)
        return False, round((time.monotonic() - started) * 1000)

    @staticmethod
    def _fingerprint(snapshot: list[Evidence]) -> str:
        """快照指纹：A/B/C 复用同一份证据的技术保证。"""
        payload = json.dumps(
            [item.model_dump(mode="json") for item in snapshot],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---- 各实验臂 ------------------------------------------------------------

    def _run_arm(
        self,
        arm: str,
        scenario: Scenario,
        snapshot: list[Evidence],
        fingerprint: str,
        extra_collects: int,
        timings: dict,
    ) -> dict:
        budget = Budget(
            self.settings.evaluation_budget_seconds, self.settings.evaluation_max_model_calls
        )
        timeout = self.settings.evaluation_model_timeout
        payload = {"evidence": [item.model_dump(mode="json") for item in snapshot]}
        required = required_evidence_ids(snapshot)
        record: dict = {"extra_collects": extra_collects}
        try:
            if arm == "A":
                record.update(self._arm_a(payload, snapshot, required, budget, timeout))
            elif arm == "B":
                record.update(self._arm_b(payload, snapshot, required, budget, timeout))
            else:
                raise DomainError("EVALUATION_ARM_UNSUPPORTED", "该实验臂尚未实现", 409)
        except Exception as error:
            # 任何异常都要留下记录：失败样本不能凭空消失（§9.3）。
            code = error.code if isinstance(error, DomainError) else type(error).__name__
            message = error.message if isinstance(error, DomainError) else str(error)
            # 模型调用自身的失败已由各臂记录（次数与耗时都是真实值）；
            # 这里兜底的是臂外的意外异常，不重复计费。
            record = {
                **record,
                "model_calls": budget.calls,
                "model_ms": record.get("model_ms", 0) or 0,
            }
            if code == "EVALUATION_BUDGET_EXCEEDED":
                record = {**record, "status": "BUDGET_EXCEEDED", "message": message}
            elif code == "AI_INVALID_RESPONSE":
                record = {**record, "status": "INVALID_RESPONSE", "message": message}
            else:
                record = {**record, "status": "ERROR", "code": code, "message": message}
        record.setdefault("status", "COMPLETED")
        record["timing"] = {
            "model_ms": record.get("model_ms", 0),
            "collect_ms": timings["collect_ms"],
            "container_ms": timings["container_ms"],
            "restore_ms": timings["restore_ms"],
            "approval_ms": None,
            # 臂内耗时：只含该臂自己的模型调用。
            "total_ms": budget.elapsed_ms,
            # 实验总耗时：加上两臂共用的快照采集与容器启停，避免被误读成「整个实验只要 1ms」。
            "experiment_total_ms": (
                budget.elapsed_ms
                + timings["collect_ms"]
                + timings["container_ms"]
                + timings["restore_ms"]
            ),
        }
        record["approval_applicable"] = False
        record["lab_restored"] = timings["lab_restored"]
        return self._persist(arm, scenario, snapshot, fingerprint, record)

    def _arm_a(
        self,
        payload: dict,
        snapshot: list[Evidence],
        required: list[str],
        budget: Budget,
        timeout: int,
    ) -> dict:
        """A 臂：单次诊断，纯文本证据。"""
        trace = ModelTrace()
        budget.check()
        try:
            diagnosis = self.provider.diagnose_domain(
                payload,
                required_ids=required,
                timeout=timeout,
                trace=trace,
                # 版本与工具权限已在实验开始时核验过（ensure_capabilities）。
                verify_capabilities=False,
            )
        except Exception as error:
            return self._call_failed(error, budget, trace)
        budget.charge()
        return {
            "status": "COMPLETED",
            "root_cause": diagnosis.root_cause,
            "evidence_sufficient": diagnosis.evidence_sufficient,
            "summary": diagnosis.summary,
            "evidence_ids": list(diagnosis.evidence_ids),
            "confidence": diagnosis.confidence,
            "model_calls": budget.calls,
            "model_ms": trace.latency_ms,
            "usage": trace.usage,
            "contract_violations": self._violations(diagnosis, snapshot),
        }

    @staticmethod
    def _call_failed(error: Exception, budget: Budget, trace: ModelTrace) -> dict:
        """模型调用失败：请求已经发出，次数与耗时都要算，token 仍是未知。

        不把失败记成「0 次调用、0 毫秒」：那是伪造的实测值（§9.3）。
        """
        code = error.code if isinstance(error, DomainError) else type(error).__name__
        message = error.message if isinstance(error, DomainError) else str(error)
        budget.charge()
        status = (
            "BUDGET_EXCEEDED"
            if code == "EVALUATION_BUDGET_EXCEEDED"
            else ("INVALID_RESPONSE" if code == "AI_INVALID_RESPONSE" else "ERROR")
        )
        return {
            "status": status,
            "code": code,
            "message": message,
            "model_calls": budget.calls,
            "model_ms": trace.latency_ms,
            "usage": trace.usage,
            "usage_partial": True,
        }

    def _arm_b(
        self,
        payload: dict,
        snapshot: list[Evidence],
        required: list[str],
        budget: Budget,
        timeout: int,
    ) -> dict:
        """B 臂：多 Agent 诊断与复核。

        复核规则（先定义清楚再实现，见 §4.5）：
        第二个 Agent 独立审阅第一次的结论，给出「同意 / 质疑 + 理由」；
        同意则沿用第一次的根因，质疑则以复核方的根因为准。
        """
        first_trace = ModelTrace()
        budget.check()
        try:
            first = self.provider.diagnose_domain(
                payload,
                required_ids=required,
                timeout=timeout,
                trace=first_trace,
                verify_capabilities=False,
            )
        except Exception as error:
            return self._call_failed(error, budget, first_trace)
        budget.charge()

        review_trace = ModelTrace()
        budget.check()
        try:
            review = self.provider.review_domain(
                {**payload, "previous_diagnosis": first.model_dump(mode="json")},
                required_ids=required,
                timeout=timeout,
                trace=review_trace,
                verify_capabilities=False,
            )
        except Exception as error:
            # 复核失败也要留下第一次的原始结论：失败与原始输出全部落库，不美化（§9.3）。
            budget.charge()  # 复核请求已经发出：次数要算上
            code = error.code if isinstance(error, DomainError) else type(error).__name__
            return {
                "status": "ERROR",
                "code": code,
                "message": error.message if isinstance(error, DomainError) else str(error),
                "root_cause": first.root_cause,
                "evidence_sufficient": first.evidence_sufficient,
                "summary": first.summary,
                "evidence_ids": list(first.evidence_ids),
                "confidence": first.confidence,
                "model_calls": budget.calls,
                "model_ms": first_trace.latency_ms + review_trace.latency_ms,
                "usage": first_trace.usage,
                "usage_partial": True,
                "contract_violations": self._violations(first, snapshot),
                "adjudication": {
                    "rule": "复核失败，未裁决；保留第一次诊断的原始结论",
                    "first": first.model_dump(mode="json"),
                    "review": None,
                },
            }
        budget.charge()
        merged_usage, usage_partial = merge_usage(first_trace.usage, review_trace.usage)

        if review.verdict == "agree":
            root_cause = first.root_cause
            sufficient = bool(first.evidence_sufficient and review.evidence_sufficient)
            rule = "agree:沿用第一次诊断的根因，证据充分性取两次判断的合取"
        else:
            root_cause = review.root_cause
            sufficient = bool(review.evidence_sufficient)
            rule = "disagree:以复核方的根因与证据充分性为准"
        return {
            "status": "COMPLETED",
            "root_cause": root_cause,
            "evidence_sufficient": sufficient,
            "summary": review.reason,
            "evidence_ids": list(dict.fromkeys([*first.evidence_ids, *review.evidence_ids])),
            "confidence": first.confidence,
            "model_calls": budget.calls,
            "model_ms": first_trace.latency_ms + review_trace.latency_ms,
            "usage": merged_usage,
            "usage_partial": usage_partial,
            "contract_violations": [
                *self._violations(first, snapshot),
                *self._violations(review, snapshot),
            ],
            "adjudication": {
                "rule": rule,
                "first": first.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
            },
        }

    @staticmethod
    def _violations(conclusion, snapshot: list[Evidence]) -> list[str]:
        """盲诊只查引用完整性：答错与拒答不拦，引用不实才记违约。"""
        try:
            validate_evidence_integrity(conclusion, snapshot)
        except DomainError as error:
            return [error.code]
        return []

    # ---- 落库 ----------------------------------------------------------------

    def _persist(
        self,
        arm: str,
        scenario: Scenario,
        snapshot: list[Evidence],
        fingerprint: str,
        record: dict,
    ) -> dict:
        truth = SCENARIOS[scenario].root_cause
        fault_present = 1 if fault_observed(snapshot, scenario) else 0
        root_cause = record.get("root_cause")
        sufficient = record.get("evidence_sufficient")
        status = record.get("status", "COMPLETED")
        if status in EXCLUDED_STATUS:
            correct, rejected = None, 0
        else:
            correct = 1 if (root_cause == truth and sufficient is True) else 0
            rejected = 1 if root_cause == "INSUFFICIENT_EVIDENCE" else 0
        run = EvaluationRun(
            id=new_id("evalrun"),
            arm=arm,
            truth_scenario=scenario.value,
            snapshot_hash=fingerprint,
            evidence_json={"items": [item.model_dump(mode="json") for item in snapshot]},
            diagnosis_json=record,
            correct=correct,
            rejected=rejected,
            fault_present=fault_present,
            model_calls=record.get("model_calls"),
            latency_ms=record.get("timing", {}).get("total_ms"),
            token_usage=record.get("usage"),
            created_at=now(),
        )
        arm_row = EvaluationArm(
            id=new_id("evalarm"),
            snapshot_hash=fingerprint,
            model=self.settings.holmes_model or "service_default",
            prompt_version=PROMPT_VERSION,
            max_tokens=None,
            timeout_s=self.settings.evaluation_model_timeout,
            arm=arm,
        )
        self.store.save_evaluation_run(run, arm_row)
        return {
            "id": run.id,
            "arm": arm,
            "snapshot_hash": fingerprint,
            "status": status,
            "root_cause": root_cause,
            "correct": correct,
            "rejected": rejected,
            "fault_present": fault_present,
            "model_calls": run.model_calls,
            "latency_ms": run.latency_ms,
        }
