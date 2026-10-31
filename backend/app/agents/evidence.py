import hashlib
import re

from app.config import ROOT, DomainError, Settings
from app.integrations.docker import DockerController
from app.integrations.probes import Probes
from app.scenarios import SCENARIOS
from app.schemas.domain import Diagnosis, Evidence, Probe, Scenario, Verdict


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


def validate_diagnosis(
    diagnosis: Diagnosis, evidence: list[Evidence], scenario: Scenario = Scenario.MYSQL_DOWN
) -> None:
    ids = {item.id for item in evidence}
    if not set(diagnosis.evidence_ids) <= ids:
        raise DomainError("EVIDENCE_CONFLICT", "模型引用了不存在的证据")
    required = {item.id for item in evidence if item.kind in {"container_state", "probe"}}
    if not required <= set(diagnosis.evidence_ids):
        raise DomainError("EVIDENCE_CONFLICT", "诊断必须引用容器状态与必需探针证据")
    if (
        not diagnosis.evidence_sufficient
        or diagnosis.root_cause != SCENARIOS[scenario].root_cause
        or not fault_observed(evidence, scenario)
    ):
        raise DomainError("EVIDENCE_INSUFFICIENT", "根因与实测证据不足以支持恢复操作")
