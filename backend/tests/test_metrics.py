"""端到端指标与时间拆分（docs/落地执行计划.md §9.1 / §9.2）。

要点：
- 错误处置提议数按「提议了非法动作/非法目标」计，**被拦下的也要计数**；
- 拦截率单独报，这是安全价值；
- 纯诊断路径不产生处置提议，指标必须显示「不适用」而不是 0；
- 时间必须拆开显示；token 取不到就标「未知」。
"""

from app.schemas.domain import Action, Plan, Scenario
from app.services.executor import plan_violations
from conftest import approve, injected, waiting


def summary(client):
    return client.get("/api/evaluation/summary").json()


def test_illegal_proposal_is_counted_even_when_blocked(client, world):
    world.action = "START_DEMO_REDIS"  # 跨场景动作：会被白名单门禁拦下
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "WAITING_APPROVAL"
    assert item["detail"]["plan"]["action_id"] == "START_DEMO_MYSQL"
    metrics = summary(client)["end_to_end"]
    assert metrics["applicable"] is True
    # 两次非法提议都被拦住，随后才出现合法的注册恢复动作。
    assert metrics["proposals"] == 3
    assert metrics["illegal_proposals"] == 2
    assert metrics["blocked_proposals"] == 2
    assert metrics["block_rate"] == 1.0
    assert metrics["illegal_proposal_rate"] == 0.6667


def test_legal_proposal_is_not_counted_as_illegal(client, world):
    incident_id, plan_id = waiting(client)
    approve(client, incident_id, plan_id)
    metrics = summary(client)["end_to_end"]
    assert metrics["proposals"] == 1
    assert metrics["illegal_proposals"] == 0
    assert metrics["illegal_proposal_rate"] == 0.0
    assert metrics["block_rate"] is None  # 没有非法提议，拦截率不适用
    assert metrics["executed"] == 1
    assert metrics["recovery_success_rate"] == 1.0
    # 诊断与规划各一次模型调用。
    assert metrics["model_calls"] == 2


def test_pure_diagnosis_metrics_stay_not_applicable(client):
    assert summary(client)["proposal_metrics_applicable"] is False
    assert any("不记 0" in note for note in summary(client)["notes"])


def test_summary_notes_state_the_honesty_limits(client):
    notes = " ".join(summary(client)["notes"])
    assert "复现基线" in notes
    assert "统计显著性" in notes
    assert "假设空间" in notes
    assert "不适用" in notes


def test_time_is_split_by_phase(client, world):
    trigger = client.post(
        "/api/evaluation/runs",
        json={"scenario": "MYSQL_DOWN", "arms": ["A", "B"], "confirm": True},
        headers={"Idempotency-Key": "TEST-eval-timing"},
    )
    assert trigger.status_code == 202
    arm = summary(client)["arms"][0]
    timing = arm["timing"]
    assert set(timing) == {
        "model_ms",
        "collect_ms",
        "container_ms",
        "restore_ms",
        "approval_ms",
        "approval_applicable",
        "experiment_total_ms",
    }
    # 纯诊断对照没有审批环节：不适用，不是 0。
    assert timing["approval_ms"] is None
    assert timing["approval_applicable"] is False
    assert timing["model_ms"] is not None
    assert timing["collect_ms"] is not None
    assert timing["container_ms"] is not None
    # 实验总耗时要把共用的采集与容器启停算进去，不能被误读成「整个实验只要几毫秒」。
    assert timing["experiment_total_ms"] >= timing["collect_ms"]


def test_token_usage_is_unknown_when_not_returned(client, world):
    client.post(
        "/api/evaluation/runs",
        json={"scenario": "MYSQL_DOWN", "arms": ["A"], "confirm": True},
        headers={"Idempotency-Key": "TEST-eval-token"},
    )
    arm = summary(client)["arms"][0]
    # TEST/MOCK 不返回 usage：必须标未知，不能拿 0 顶替。
    assert arm["token_usage_total"] is None
    assert arm["token_usage_known"] == 0


def test_plan_violations_matches_the_gate():
    legal = Plan(
        action_id="START_DEMO_MYSQL",
        target="demo-mysql",
        reason="TEST/MOCK",
        risk="LOW",
        expected_checks=["mysql_select_1", "app_db_check"],
    )
    assert plan_violations(legal, Scenario.MYSQL_DOWN) == []
    cross = legal.model_copy(update={"action_id": Action.START_DEMO_REDIS})
    assert plan_violations(cross, Scenario.MYSQL_DOWN)
    wrong_target = legal.model_copy(update={"target": "demo-redis"})
    assert plan_violations(wrong_target, Scenario.MYSQL_DOWN)
    wrong_checks = legal.model_copy(update={"expected_checks": ["nginx_hello"]})
    assert plan_violations(wrong_checks, Scenario.MYSQL_DOWN)
