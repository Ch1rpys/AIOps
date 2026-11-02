import hashlib
import re

from app.config import ROOT, SERVICES, DomainError, Settings
from app.integrations.docker import DockerController
from app.integrations.probes import Probes
from app.scenarios import SCENARIOS
from app.schemas.domain import (
    Diagnosis,
    Evidence,
    Probe,
    Scenario,
    Verdict,
    required_evidence_ids,
)


class EvidenceAgent:
    def __init__(self, settings: Settings, docker: DockerController, probes: Probes):
        self.settings, self.docker, self.probes = settings, docker, probes

    def redact(self, text: str) -> str:
        for secret in (
            self.settings.demo_mysql_password,
            self.settings.demo_mysql_root_password,
            self.settings.holmes_api_key,
            self.settings.planner_api_key,
            self.settings.admin_token,
        ):
            if secret.get_secret_value():
                text = text.replace(secret.get_secret_value(), "[REDACTED]")
        return re.sub(
            r"(?i)(password|api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", text
        )

    def collect(self, scenario: Scenario = Scenario.MYSQL_DOWN) -> list[Evidence]:
        definition = SCENARIOS[scenario]
        snapshot = self.docker.snapshot()
        evidence = [Evidence(source="docker-compose", kind="container_state", raw=snapshot)]
        for probe in self.probes.for_scenario(scenario):
            evidence.append(
                Evidence(source=probe.name, kind="probe", raw=probe.model_dump(mode="json"))
            )
        for service in definition.logs:
            evidence.append(
                Evidence(
                    source=service,
                    kind="logs",
                    raw={"text": self.redact(self.docker.logs(service))},
                )
            )
        path = ROOT / f"knowledge/runbooks/{definition.runbook}.md"
        content = path.read_text(encoding="utf-8")
        evidence.append(
            Evidence(
                source=f"knowledge/runbooks/{definition.runbook}.md",
                kind="runbook",
                raw={"sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content},
            )
        )
        return evidence

    def collect_domain(self) -> list[Evidence]:
        """盲诊采证：四个服务全量观测，不带任何场景标识。

        与 collect(scenario) 并列而不是替换：
        - 不按场景挑选服务与探针，改为容器状态 + 全部基线探针 + 四个服务日志；
        - 只挂通用 Runbook，不挂任何场景专属 Runbook；
        - 不传 scenario_key / required_checks / allowed_action / target。
        """
        snapshot = self.docker.snapshot()
        evidence = [Evidence(source="docker-compose", kind="container_state", raw=snapshot)]
        for probe in self.probes.baseline():
            evidence.append(
                Evidence(source=probe.name, kind="probe", raw=probe.model_dump(mode="json"))
            )
        for service in SERVICES:
            evidence.append(
                Evidence(
                    source=service,
                    kind="logs",
                    raw={"text": self.redact(self.docker.logs(service))},
                )
            )
        path = ROOT / "knowledge/runbooks/general.md"
        content = path.read_text(encoding="utf-8")
        evidence.append(
            Evidence(
                source="knowledge/runbooks/general.md",
                kind="runbook",
                raw={"sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content},
            )
        )
        return evidence


def fault_observed(evidence: list[Evidence], scenario: Scenario = Scenario.MYSQL_DOWN) -> bool:
    definition = SCENARIOS[scenario]
    states = [e.raw for e in evidence if e.kind == "container_state"]
    probes = [Probe.model_validate(e.raw) for e in evidence if e.kind == "probe"]
    return (
        bool(states)
        and states[-1].get(definition.target, {}).get("state") == "exited"
        and (
            scenario != Scenario.NGINX_502
            or any(p.name == "nginx_hello" and p.observed.get("status_code") == 502 for p in probes)
        )
        and {p.name for p in probes if p.verdict == Verdict.FAIL} >= set(definition.checks)
    )


def observations_available(
    evidence: list[Evidence], scenario: Scenario = Scenario.MYSQL_DOWN
) -> bool:
    definition = SCENARIOS[scenario]
    states = [item.raw for item in evidence if item.kind == "container_state"]
    probes = [Probe.model_validate(item.raw) for item in evidence if item.kind == "probe"]
    return (
        bool(states)
        and bool(states[-1].get(definition.target))
        and {probe.name for probe in probes if probe.verdict != Verdict.UNKNOWN}
        >= set(definition.checks)
    )


def validate_evidence_integrity(diagnosis: Diagnosis, evidence: list[Evidence]) -> None:
    """引用完整性：evidence id 必须真实存在，且必须覆盖容器状态与全部探针证据。

    闭环入口与盲诊评测都调用。判据只取决于证据本身，不涉及场景注册表，
    因此盲诊可以产出「答错」与「拒答」的结论而不被拦截。
    """
    ids = {item.id for item in evidence}
    if not set(diagnosis.evidence_ids) <= ids:
        raise DomainError("EVIDENCE_CONFLICT", "模型引用了不存在的证据")
    if not set(required_evidence_ids(evidence)) <= set(diagnosis.evidence_ids):
        raise DomainError("EVIDENCE_CONFLICT", "诊断必须引用容器状态与必需探针证据")


def validate_execution_preconditions(
    diagnosis: Diagnosis, evidence: list[Evidence], scenario: Scenario = Scenario.MYSQL_DOWN
) -> None:
    """执行许可：evidence_sufficient 为真、根因命中注册值、且实测故障成立。

    只有具备执行许可的闭环入口调用；盲诊评测路径不得调用。
    注意 evidence_sufficient 属于执行许可而非引用完整性：
    模型自报「证据不足」时必须拦下，不能放行到审批页。
    """
    if (
        not diagnosis.evidence_sufficient
        or diagnosis.root_cause != SCENARIOS[scenario].root_cause
        or not fault_observed(evidence, scenario)
    ):
        raise DomainError("EVIDENCE_INSUFFICIENT", "根因与实测证据不足以支持恢复操作")


def validate_diagnosis(
    diagnosis: Diagnosis, evidence: list[Evidence], scenario: Scenario = Scenario.MYSQL_DOWN
) -> None:
    """闭环入口的完整门禁：先引用完整性，后执行许可。

    拆分后闭环行为逐字不变：EVIDENCE_CONFLICT 先于 EVIDENCE_INSUFFICIENT 抛出。
    """
    validate_evidence_integrity(diagnosis, evidence)
    validate_execution_preconditions(diagnosis, evidence, scenario)
