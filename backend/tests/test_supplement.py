"""M3：候选根因与受限主动补采（docs/落地执行计划.md §6）。

三条硬边界：
- 模型只能从注册的检查 ID 里选要补什么，不能自由发挥；
- 补采轮次与总耗时都有上限，超限如实记录而不是静默截断；
- 补采只增加观测，不增加任何执行权限。
"""

import pytest
from app.config import CHECK_IDS
from app.schemas.domain import CheckRequest
from conftest import injected


def detail(client, incident_id):
    return client.get(f"/api/incidents/{incident_id}").json()


def test_requested_check_is_collected_and_diagnosis_runs_again(client, world):
    world.requested_checks = [[{"check_id": "app_health", "reason": "需要确认应用自身是否健康"}]]
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = detail(client, incident_id)
    assert len(world.diagnosis_contexts) == 2
    assert item["status"] == "WAITING_APPROVAL"
    supplement = item["detail"]["supplement"]
    assert supplement["requested"] == [
        {"check_id": "app_health", "reason": "需要确认应用自身是否健康"}
    ]
    assert [entry["source"] for entry in supplement["collected"]] == ["app_health"]
    # 补采只增加观测，不产生任何容器动作。
    assert world.operations == [("demo-mysql", "stop")]


def test_supplement_evidence_must_be_cited_by_the_next_diagnosis(client, world):
    world.requested_checks = [[{"check_id": "app_health", "reason": "补一条应用健康观测"}]]
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    second = world.diagnosis_contexts[-1]["evidence"]
    assert any(entry["source"] == "app_health" for entry in second)
    # 第二次诊断必须引用补采进来的证据，否则完整性校验会拦下。
    diagnosis = detail(client, incident_id)["detail"]["diagnosis"]
    assert any(entry["id"] == diagnosis["evidence_ids"][-1] for entry in second)


def test_unregistered_check_is_never_collected(client, world):
    incident_id = injected(client)
    service = client.app.state.platform
    state = {
        "diagnosis": {"requested_checks": [{"check_id": "rm -rf /", "reason": "自由发挥"}]},
        "evidence": [],
    }
    assert service.workflow.pending_requests(state) == []
    assert detail(client, incident_id)["status"] == "INJECTED"


def test_already_collected_check_is_not_collected_again(client, world):
    injected(client)
    service = client.app.state.platform
    state = {
        "diagnosis": {"requested_checks": [{"check_id": "mysql_select_1", "reason": "再看一次"}]},
        "evidence": [
            {"id": "ev_1", "source": "mysql_select_1", "kind": "probe", "raw": {}},
        ],
    }
    assert service.workflow.pending_requests(state) == []


def test_supplement_rounds_are_capped(client, world):
    world.requested_checks = [
        [{"check_id": "app_health", "reason": "补一条"}],
        [{"check_id": "nginx_hello", "reason": "再补一条"}],
        [{"check_id": "app_cache_check", "reason": "还想看"}],
    ]
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = detail(client, incident_id)
    # 最多两轮补采：初次诊断 + 两轮补采 = 三次诊断。
    assert len(world.diagnosis_contexts) == 3
    assert item["status"] == "WAITING_APPROVAL"
    assert item["detail"]["supplement_stopped"] == "已达补采轮次上限"


def test_supplement_budget_stops_collection(client, world):
    world.requested_checks = [[{"check_id": "app_health", "reason": "补一条"}]]
    client.app.state.platform.workflow.settings.supplement_budget_seconds = 0
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = detail(client, incident_id)
    assert len(world.diagnosis_contexts) == 1
    assert item["detail"]["supplement_stopped"] == "补采耗时预算已用尽"


def test_only_registered_checks_can_be_requested():
    assert set(CHECK_IDS) == {
        "mysql_select_1",
        "app_db_check",
        "redis_roundtrip",
        "app_cache_check",
        "nginx_hello",
        "app_health",
    }
    CheckRequest(check_id="nginx_hello", reason="看代理")
    with pytest.raises(ValueError):
        CheckRequest(check_id="docker exec", reason="自由发挥")
