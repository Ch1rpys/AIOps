"""拆分校验后的护栏：引用完整性与执行许可必须各司其职。

拆分自 `validate_diagnosis`（见 docs/落地执行计划.md §4.2）。
其中 `evidence_sufficient` 属于执行许可：漏掉它会让闭环放行
「模型自认证据不足」的诊断，因此这里用独立用例钉住。
"""

import pytest
from app.agents.evidence import (
    EvidenceAgent,
    validate_evidence_integrity,
    validate_execution_preconditions,
)
from app.config import DomainError
from app.schemas.domain import Diagnosis, Evidence, Probe, Scenario, Verdict
from conftest import injected


def evidence_set(include_probes=True, include_state=True):
    items = []
    if include_state:
        items.append(
            Evidence(
                id="ev_state",
                source="docker-compose",
                kind="container_state",
                raw={"demo-mysql": {"state": "exited"}},
            )
        )
    if include_probes:
        for name in ("mysql_select_1", "app_db_check"):
            items.append(
                Evidence(
                    id=f"ev_{name}",
                    source=name,
                    kind="probe",
                    raw=Probe(
                        name=name,
                        verdict=Verdict.FAIL,
                        observed={"mysql_error": 2003},
                    ).model_dump(mode="json"),
                )
            )
    return items


def diagnosis(**overrides):
    payload = {
        "root_cause": "MYSQL_SERVICE_STOPPED",
        "summary": "TEST/MOCK",
        "evidence_ids": ["ev_state", "ev_mysql_select_1", "ev_app_db_check"],
        "confidence": 0.9,
        "evidence_sufficient": True,
    }
    payload.update(overrides)
    return Diagnosis.model_validate(payload)


def test_integrity_rejects_unknown_evidence_id():
    with pytest.raises(DomainError) as error:
        validate_evidence_integrity(
            diagnosis(evidence_ids=["ev_state", "ev_mysql_select_1", "ev_ghost"]), evidence_set()
        )
    assert error.value.code == "EVIDENCE_CONFLICT"


def test_integrity_requires_container_state_and_all_probes():
    with pytest.raises(DomainError) as error:
        validate_evidence_integrity(diagnosis(evidence_ids=["ev_mysql_select_1"]), evidence_set())
    assert error.value.code == "EVIDENCE_CONFLICT"
    with pytest.raises(DomainError) as error:
        validate_evidence_integrity(diagnosis(evidence_ids=["ev_state"]), evidence_set())
    assert error.value.code == "EVIDENCE_CONFLICT"


def test_integrity_passes_when_every_required_evidence_is_cited():
    validate_evidence_integrity(diagnosis(), evidence_set())


def test_execution_preconditions_rejects_self_reported_insufficient_evidence():
    with pytest.raises(DomainError) as error:
        validate_execution_preconditions(
            diagnosis(evidence_sufficient=False), evidence_set(), Scenario.MYSQL_DOWN
        )
    assert error.value.code == "EVIDENCE_INSUFFICIENT"


def test_execution_preconditions_rejects_wrong_root_cause():
    with pytest.raises(DomainError) as error:
        validate_execution_preconditions(
            diagnosis(root_cause="OTHER"), evidence_set(), Scenario.MYSQL_DOWN
        )
    assert error.value.code == "EVIDENCE_INSUFFICIENT"


def test_execution_preconditions_requires_observed_fault():
    healthy = evidence_set()
    healthy[1] = Evidence(
        id="ev_mysql_select_1",
        source="mysql_select_1",
        kind="probe",
        raw=Probe(name="mysql_select_1", verdict=Verdict.PASS, observed={"row": [1]}).model_dump(
            mode="json"
        ),
    )
    with pytest.raises(DomainError) as error:
        validate_execution_preconditions(diagnosis(), healthy, Scenario.MYSQL_DOWN)
    assert error.value.code == "EVIDENCE_INSUFFICIENT"


def test_execution_preconditions_accepts_matching_evidence():
    validate_execution_preconditions(diagnosis(), evidence_set(), Scenario.MYSQL_DOWN)


def test_blind_path_keeps_wrong_answers_instead_of_blocking_them():
    """盲诊只查完整性：答错与拒答必须能落库，否则正确率无从统计。"""
    validate_evidence_integrity(
        diagnosis(root_cause="OTHER", evidence_sufficient=False), evidence_set()
    )


def test_closed_loop_blocks_self_reported_insufficient_evidence(client, world):
    """闭环入口必须拦下 evidence_sufficient=false 的诊断，不放行到审批页。"""
    world.evidence_sufficient = False
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "UNKNOWN"
    assert item["detail"]["error"]["code"] == "EVIDENCE_INSUFFICIENT"
    assert world.operations == [("demo-mysql", "stop")]


def test_evidence_agent_collects_domain_evidence_without_scenario_markers(settings, world):
    """盲诊采证只做全量观测，不带入场景标识。"""
    agent = EvidenceAgent(settings, world, world)
    items = agent.collect_domain()
    kinds = [item.kind for item in items]
    assert kinds.count("container_state") == 1
    assert kinds.count("runbook") == 1
    assert kinds.count("logs") == 4
    assert kinds.count("probe") == 6
    sources = {item.source for item in items}
    assert "knowledge/runbooks/general.md" in sources
    assert not any("mysql.md" in source or "redis.md" in source for source in sources)
