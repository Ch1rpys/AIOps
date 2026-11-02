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


class Budget:
    """§9.4 的预算口径：预算是**上限**不是配额。

    超限时终止本次实验并记录（记为该臂的一个观测样本），不是静默截断。
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
            finally:
                self.store.release_evaluation_lease(holder)

    def busy(self) -> bool:
        return self.store.evaluation_busy()

    # ---- 实验主流程 ----------------------------------------------------------

    def _experiment(self, scenario: Scenario, arms: list[str]) -> dict:
        baseline = lab_baseline(self.docker, self.probes)
        if not baseline["healthy"]:
            raise DomainError("BASELINE_UNHEALTHY", "四个 demo 服务与探针未全部通过，禁止评测", 409)
        container_started = time.monotonic()
        self.docker.change(SCENARIOS[scenario].target, "stop")
        container_ms = round((time.monotonic() - container_started) * 1000)

        snapshot, extra_collects, collect_ms = self._capture_snapshot(scenario)
        fingerprint = self._fingerprint(snapshot)
        restored, restore_ms = self._restore(scenario)
        if not restored:
            logger.error("evaluation_lab_not_restored fingerprint=%s", fingerprint)

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
        except DomainError as error:
            if error.code == "EVALUATION_BUDGET_EXCEEDED":
                record = {**record, "status": "BUDGET_EXCEEDED", "message": error.message}
            elif error.code == "AI_INVALID_RESPONSE":
                record = {**record, "status": "INVALID_RESPONSE", "message": error.message}
            else:
                record = {**record, "status": "ERROR", "code": error.code, "message": error.message}
        record.setdefault("status", "COMPLETED")
        record["timing"] = {
            "model_ms": record.get("model_ms", 0),
            "collect_ms": timings["collect_ms"],
            "container_ms": timings["container_ms"],
            "restore_ms": timings["restore_ms"],
            "approval_ms": None,
            "total_ms": budget.elapsed_ms,
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
        diagnosis = self.provider.diagnose_domain(
            payload, required_ids=required, timeout=timeout, trace=trace
        )
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
        first = self.provider.diagnose_domain(
            payload, required_ids=required, timeout=timeout, trace=first_trace
        )
        budget.charge()

        review_trace = ModelTrace()
        budget.check()
        try:
            review = self.provider.review_domain(
                {**payload, "previous_diagnosis": first.model_dump(mode="json")},
                required_ids=required,
                timeout=timeout,
                trace=review_trace,
            )
        except DomainError as error:
            # 复核失败也要留下第一次的原始结论：失败与原始输出全部落库，不美化（§9.3）。
            return {
                "status": "ERROR",
                "code": error.code,
                "message": error.message,
                "root_cause": first.root_cause,
                "evidence_sufficient": first.evidence_sufficient,
                "summary": first.summary,
                "evidence_ids": list(first.evidence_ids),
                "confidence": first.confidence,
                "model_calls": budget.calls,
                "model_ms": first_trace.latency_ms,
                "usage": first_trace.usage,
                "contract_violations": self._violations(first, snapshot),
                "adjudication": {
                    "rule": "复核失败，未裁决；保留第一次诊断的原始结论",
                    "first": first.model_dump(mode="json"),
                    "review": None,
                },
            }
        budget.charge()

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
            "usage": first_trace.usage or review_trace.usage,
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
