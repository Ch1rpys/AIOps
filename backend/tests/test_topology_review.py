"""M4：显式拓扑、影响图与复盘摘要（docs/落地执行计划.md §7）。

重点是三类关系不能混淆：
已观测异常（有实测支撑）、配置依赖（启动顺序）、推断传播（沿业务边推断）。
"""

from app.services.topology import (
    CONFIGURED,
    INFERRED,
    OBSERVED,
    load_topology,
    render_general_runbook,
)
from conftest import approve, injected, waiting


def test_runbook_is_generated_from_topology():
    """通用 Runbook 与影响图共用同一份拓扑：产物必须与拓扑一致，不允许各写一份。"""
    path = load_topology()
    assert path is not None
    from app.services.topology import RUNBOOK_PATH

    assert RUNBOOK_PATH.read_text(encoding="utf-8") == render_general_runbook()


def test_topology_distinguishes_startup_order_from_business_dependency():
    topology = load_topology()
    startup = {(edge["from"], edge["to"]) for edge in topology.startup_edges()}
    business = {(edge["from"], edge["to"]) for edge in topology.business_edges()}
    assert ("demo-app", "demo-mysql") in business
    assert ("demo-app", "demo-mysql") in startup
    # /api/hello 与 /health 不依赖数据库或缓存：这是「启动顺序 ≠ 业务因果」的反例。
    assert topology.needed_by("demo-mysql") == ["/db-check"]
    assert topology.needed_by("demo-redis") == ["/cache-check"]
    assert "/health" not in topology.needed_by("demo-mysql")


def test_general_runbook_has_no_scenario_markers():
    content = render_general_runbook()
    for token in (
        "MYSQL_DOWN",
        "REDIS_DOWN",
        "NGINX_502",
        "MYSQL_SERVICE_STOPPED",
        "REDIS_SERVICE_STOPPED",
        "UPSTREAM_APP_STOPPED",
        "START_DEMO_MYSQL",
        "START_DEMO_REDIS",
        "START_DEMO_APP",
    ):
        assert token not in content


def test_impact_graph_separates_the_three_relation_kinds(client, world):
    incident_id = injected(client)
    graph = client.get(f"/api/incidents/{incident_id}/impact").json()
    kinds = {edge["kind"] for edge in graph["edges"]}
    assert CONFIGURED in kinds
    assert INFERRED in kinds
    assert graph["legend"][OBSERVED].startswith("已观测异常")
    assert graph["legend"][CONFIGURED].startswith("配置依赖")
    assert graph["legend"][INFERRED].startswith("推断传播")
    # 故障已注入：demo-mysql 是实测异常，/db-check 受影响，/health 不受影响。
    mysql = next(node for node in graph["nodes"] if node["id"] == "demo-mysql")
    assert mysql["observed_anomaly"] is True
    assert mysql["state"] == "exited"
    interfaces = {item["path"]: item for item in graph["interfaces"]}
    assert interfaces["/db-check"]["impacted"] is True
    assert interfaces["/cache-check"]["impacted"] is False
    assert interfaces["/health"]["impacted"] is False
    # 启动顺序边只是配置依赖，不能被当成推断传播。
    startup = [edge for edge in graph["edges"] if edge["kind"] == CONFIGURED]
    assert all(edge["inferred"] is False for edge in startup)


def test_inferred_propagation_requires_a_diagnosed_root_cause(client, world):
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    graph = client.get(f"/api/incidents/{incident_id}/impact").json()
    assert graph["root_cause"] == "MYSQL_SERVICE_STOPPED"
    inferred = [edge for edge in graph["edges"] if edge["kind"] == INFERRED]
    assert any(edge["inferred"] is True for edge in inferred)
    # 业务边 demo-app → demo-mysql 有实测支撑；demo-nginx → demo-app 没有。
    assert next(edge for edge in inferred if edge["to"] == "demo-mysql")["inferred"] is True
    assert next(edge for edge in inferred if edge["to"] == "demo-app")["inferred"] is False


def test_review_summary_comes_from_persisted_records(client, world):
    incident_id, plan_id = waiting(client)
    approve(client, incident_id, plan_id)
    review = client.get(f"/api/incidents/{incident_id}/review").json()
    assert review["source"] == "incident_records"
    assert review["root_cause"] == "MYSQL_SERVICE_STOPPED"
    assert review["root_cause_evidence_ids"]
    assert review["approved_action"]["plan"]["action_id"] == "START_DEMO_MYSQL"
    assert review["approved_action"]["approval"]["decision"] == "approve"
    assert review["probes_before"]
    assert review["probes_after"]
    assert review["unresolved"] == []
    assert review["audit_appendix"]
    assert any(interface["path"] == "/db-check" for interface in review["affected_interfaces"])


def test_review_keeps_unresolved_items(client, world):
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    review = client.get(f"/api/incidents/{incident_id}/review").json()
    assert review["status"] == "WAITING_APPROVAL"
    assert review["approved_action"]["approval"] is None
    assert review["approved_action"]["plan"]["action_id"] == "START_DEMO_MYSQL"
    assert "计划已生成，等待人工审批" in review["unresolved"]
    markdown = client.get(f"/api/incidents/{incident_id}/review.md")
    assert markdown.status_code == 200
    assert "不重新执行任何容器动作" in markdown.text


def test_review_of_missing_incident_is_404(client):
    assert client.get("/api/incidents/inc_missing/impact").status_code == 404
