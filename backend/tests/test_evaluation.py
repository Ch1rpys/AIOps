"""盲诊评测的护栏（docs/落地执行计划.md §4.6）。

重点不是「跑通」，而是三件事：
真值不外泄、A/B 在同一份快照上可比、失败与拒答不被美化。
"""

import base64

import pytest
from app.config import DomainError
from app.repositories.store import EvaluationRun
from app.schemas.domain import (
    VisionAnomaly,
    VisionObservation,
    now,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"TEST-MOCK-image-bytes"


def trigger(
    client,
    scenario="MYSQL_DOWN",
    arms=("A", "B"),
    key="TEST-eval-0001",
    attachment_ids=None,
):
    response = client.post(
        "/api/evaluation/runs",
        json={
            "scenario": scenario,
            "arms": list(arms),
            "confirm": True,
            "attachment_ids": list(attachment_ids or []),
        },
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def runs(client, **params):
    response = client.get("/api/evaluation/runs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_evaluation_creates_no_incident_and_never_exposes_truth(client, world):
    trigger(client)
    # 真值不能借道工单外泄：评测不创建任何普通工单。
    assert client.get("/api/incidents").json() == []
    assert world.operations == [("demo-mysql", "stop"), ("demo-mysql", "start")]
    for row in runs(client):
        assert "truth_scenario" not in row
        assert "correct" not in row
        assert "rejected" not in row
    # 真值只落在评测表里。
    store = client.app.state.platform.store
    with store.session() as session:
        truths = {item.truth_scenario for item in session.query(EvaluationRun).all()}
    assert truths == {"MYSQL_DOWN"}


def test_evaluation_view_requires_admin_token(client):
    trigger(client)
    client.headers.pop("X-Admin-Token")
    forbidden = client.get("/api/evaluation/runs", params={"view": "evaluation"})
    assert forbidden.status_code == 403
    client.headers["X-Admin-Token"] = (
        client.app.state.platform.settings.admin_token.get_secret_value()
    )
    revealed = client.get("/api/evaluation/runs", params={"view": "evaluation"}).json()
    assert {row["truth_scenario"] for row in revealed} == {"MYSQL_DOWN"}
    assert {row["correct"] for row in revealed} == {1}


@pytest.mark.parametrize(
    "scenario,target",
    [("MYSQL_DOWN", "demo-mysql"), ("REDIS_DOWN", "demo-redis"), ("NGINX_502", "demo-app")],
)
def test_every_scenario_produces_a_scored_conclusion(client, world, scenario, target):
    trigger(client, scenario=scenario, key=f"TEST-eval-{scenario}")
    rows = runs(client)
    assert {row["arm"] for row in rows} == {"A", "B"}
    for row in rows:
        assert row["status"] == "COMPLETED"
        assert row["fault_present"] == 1
        assert row["root_cause"] == "MYSQL_SERVICE_STOPPED"
    assert (target, "stop") in world.operations
    assert (target, "start") in world.operations


def test_arms_share_one_snapshot(client):
    trigger(client)
    rows = runs(client)
    assert len({row["snapshot_hash"] for row in rows}) == 1
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm["valid_runs"] == 1
    assert arm["accuracy"] == 1.0


def test_blind_path_never_calls_planner_and_carries_no_pointing_fields(client, world):
    trigger(client)
    assert world.diagnosis_contexts == []  # 闭环诊断入口没有被调用
    assert world.blind_contexts
    for context in world.blind_contexts:
        assert set(context) <= {"evidence", "previous_diagnosis"}
        text = str(context)
        for field in ("required_checks", "allowed_action", "expected_checks", "scenario_key"):
            assert field not in text


def test_rejection_is_counted_separately_and_not_as_correct(client, world):
    world.blind_root_cause = "INSUFFICIENT_EVIDENCE"
    trigger(client)
    rows = runs(client)
    assert all(row["root_cause"] == "INSUFFICIENT_EVIDENCE" for row in rows)
    revealed = client.get("/api/evaluation/runs", params={"view": "evaluation"}).json()
    assert all(row["rejected"] == 1 for row in revealed)
    assert all(row["correct"] == 0 for row in revealed)
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm["rejection_rate"] == 1.0
    assert arm["accuracy"] == 0.0


def test_wrong_root_cause_is_kept_in_full(client, world):
    world.blind_root_cause = "OTHER"
    trigger(client)
    revealed = client.get("/api/evaluation/runs", params={"view": "evaluation"}).json()
    assert all(row["correct"] == 0 for row in revealed)
    assert all(row["root_cause"] == "OTHER" for row in revealed)


def test_b_arm_review_overrules_the_first_conclusion(client, world):
    world.review_verdict = "disagree"
    world.review_root_cause = "REDIS_SERVICE_STOPPED"
    trigger(client)
    revealed = client.get("/api/evaluation/runs", params={"view": "evaluation"}).json()
    b_row = next(row for row in revealed if row["arm"] == "B")
    a_row = next(row for row in revealed if row["arm"] == "A")
    assert a_row["root_cause"] == "MYSQL_SERVICE_STOPPED"
    assert b_row["root_cause"] == "REDIS_SERVICE_STOPPED"
    detail = client.get(f"/api/evaluation/runs/{b_row['id']}").json()
    assert detail["diagnosis"]["adjudication"]["rule"].startswith("disagree")


def test_budget_exceeded_is_recorded_but_kept_out_of_the_denominator(client, world):
    service = client.app.state.evaluation
    service.settings.evaluation_budget_seconds = 0
    trigger(client)
    rows = runs(client)
    assert rows and all(row["status"] == "BUDGET_EXCEEDED" for row in rows)
    revealed = client.get("/api/evaluation/runs", params={"view": "evaluation"}).json()
    assert all(row["correct"] is None for row in revealed)
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm["budget_exceeded"] == 1
    assert arm["valid_runs"] == 0
    assert arm["accuracy"] is None


def test_snapshot_without_the_fault_is_excluded_from_the_denominator(client, world):
    trigger(client)
    store = client.app.state.platform.store
    with store.session() as session, session.begin():
        session.add(
            EvaluationRun(
                id="evalrun_invalid",
                arm="A",
                truth_scenario="MYSQL_DOWN",
                snapshot_hash="fingerprint",
                evidence_json={},
                diagnosis_json={"status": "COMPLETED", "root_cause": "MYSQL_SERVICE_STOPPED"},
                correct=1,
                rejected=0,
                fault_present=0,
                model_calls=1,
                latency_ms=10,
                token_usage=None,
                created_at=now(),
            )
        )
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm["invalid_runs"] == 1
    assert arm["valid_runs"] == 1
    assert arm["accuracy"] == 1.0


def test_summary_declares_proposal_metrics_as_not_applicable(client):
    trigger(client)
    summary = client.get("/api/evaluation/summary").json()
    assert summary["proposal_metrics_applicable"] is False
    assert any("不适用" in note for note in summary["notes"])
    assert any("统计显著性" in note for note in summary["notes"])
    assert any("收窄" in note for note in summary["notes"])


def test_workbench_injection_is_refused_while_evaluation_holds_the_lab(client):
    store = client.app.state.platform.store
    store.acquire_evaluation_lease("TEST-holder")
    response = client.post(
        "/api/scenarios/MYSQL_DOWN/inject",
        json={"confirm": True},
        headers={"Idempotency-Key": "TEST-inject-during-eval"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVALUATION_BUSY"


def test_evaluation_is_refused_while_the_workbench_holds_the_lab(client):
    from conftest import injected

    injected(client)
    response = client.post(
        "/api/evaluation/runs",
        json={"scenario": "MYSQL_DOWN", "arms": ["A"], "confirm": True},
        headers={"Idempotency-Key": "TEST-eval-during-lab"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAB_BUSY"


def test_idempotency_key_replays_the_same_evaluation_response(client):
    first = trigger(client, key="TEST-eval-same")
    second = trigger(client, key="TEST-eval-same")
    assert first == second
    assert len(runs(client)) == 2  # 只跑了一次实验，两个臂


def test_evaluation_requires_admin_token(client):
    client.headers.pop("X-Admin-Token")
    response = client.post(
        "/api/evaluation/runs",
        json={"scenario": "MYSQL_DOWN", "arms": ["A"], "confirm": True},
        headers={"Idempotency-Key": "TEST-eval-noauth"},
    )
    assert response.status_code == 403


def test_review_failure_keeps_the_first_conclusion(client, world):
    """复核失败也要留下第一次的原始结论，不能只留下一个错误码。"""
    world.blind_review_error = DomainError("AI_UNAVAILABLE", "TEST/MOCK 复核不可用", 503)
    trigger(client)
    rows = runs(client)
    assert rows
    detail = client.get(f"/api/evaluation/runs/{rows[0]['id']}").json()
    assert detail["status"] == "ERROR"
    assert detail["diagnosis"]["root_cause"] == "MYSQL_SERVICE_STOPPED"
    assert detail["diagnosis"]["adjudication"]["first"]["root_cause"] == "MYSQL_SERVICE_STOPPED"
    assert detail["diagnosis"]["adjudication"]["review"] is None
    # 基础设施故障不算模型能力失败，不进正确率分母。
    summary = client.get("/api/evaluation/summary").json()
    arm_b = next(item for item in summary["arms"] if item["arm"] == "B")
    arm_a = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm_b["errors"] >= 1
    assert arm_b["valid_runs"] == 0
    # A 臂没有受影响：同一个快照上 A 仍然可评分。
    assert arm_a["errors"] == 0
    assert arm_a["valid_runs"] == 1


def test_failed_experiment_is_recorded_not_dropped(client, world):
    """实验失败也要留下记录：不能因为失败就看不到这次尝试。"""
    world.stop_has_no_effect = True  # 注入了，但故障没出现 → 故障无法确认
    trigger(client)
    # 失败也必须把实验环境恢复健康，不能把停掉的容器留在那里。
    assert ("demo-mysql", "stop") in world.operations
    assert ("demo-mysql", "start") in world.operations
    rows = runs(client)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "ERROR"
        assert row["fault_present"] is None
    detail = client.get(f"/api/evaluation/runs/{rows[0]['id']}").json()
    assert detail["diagnosis"]["code"]
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm["errors"] >= 1
    assert arm["valid_runs"] == 0
    assert arm["accuracy"] is None


def test_capability_check_runs_once_per_experiment(client, world):
    """一次实验只核验一次版本与工具权限：否则单臂最坏耗时突破 45 秒预算。

    替身会统计「调用内部」的核验，所以这个断言对真实 provider 同样成立。
    """
    trigger(client, arms=("A", "B"))
    # 实验级核验 1 次 + A 臂 0 次 + B 臂 0 次。
    assert world.capability_checks == 1


def test_failed_run_does_not_report_zero_measurements(client, world):
    """没跑成的实验不能报「0 毫秒」：0 是伪造的实测值，不是未知（§9.3）。"""
    world.stop_has_no_effect = True  # 注入后故障没出现 → 实验失败
    trigger(client)
    rows = runs(client)
    assert rows
    for row in rows:
        assert row["latency_ms"] is None
        assert row["model_calls"] is None
        assert row["fault_present"] is None
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    # 没有可用样本时，时间与调用次数都应是「未知」，不是 0。
    assert arm["avg_latency_ms"] is None
    assert arm["model_calls_avg"] is None
    assert arm["timing"]["model_ms"] is None
    assert arm["timing"]["collect_ms"] is None
    assert arm["timing"]["experiment_total_ms"] is None
    assert arm["token_usage_total"] is None


def test_failed_model_call_keeps_its_real_cost(client, world):
    """失败的模型调用也要算调用次数与真实耗时，不能报「0 次 0 毫秒」（§9.3）。"""
    world.blind_error = DomainError("AI_UNAVAILABLE", "TEST/MOCK 模型不可用", 503)
    trigger(client, arms=("A",))
    rows = runs(client)
    assert len(rows) == 1
    detail = client.get(f"/api/evaluation/runs/{rows[0]['id']}").json()
    assert detail["status"] == "ERROR"
    # 调用确实发出去了：次数已知。
    assert detail["model_calls"] == 1
    # token 仍是未知。
    assert detail["token_usage"] is None
    summary = client.get("/api/evaluation/summary").json()
    arm = next(item for item in summary["arms"] if item["arm"] == "A")
    assert arm["model_calls_avg"] == 1
    assert arm["model_calls_unknown"] == 0
    assert arm["token_usage_total"] is None


def test_c_arm_requires_a_visual_attachment(client, world):
    """C 臂没有视觉证据就退化成 B 臂：那是假对照，明确拒绝而不是降级伪装。"""
    response = client.post(
        "/api/evaluation/runs",
        json={"scenario": "MYSQL_DOWN", "arms": ["C"], "confirm": True},
        headers={"Idempotency-Key": "TEST-eval-c-no-attachment"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVALUATION_ATTACHMENT_REQUIRED"


def test_c_arm_runs_on_the_same_snapshot_with_visual_evidence(client, world):
    """C 臂 = B 臂 + 同一份快照里的视觉证据：所有臂看到的输入完全相同。"""
    world.vision_observation = VisionObservation(
        observed_at="2026-11-02T10:00:00",
        extracted_text="503 Service Unavailable",
        anomalies=[VisionAnomaly(description="数据库错误页", region=[], text="503")],
    )
    upload = client.post(
        "/api/evaluation/attachments",
        json={
            "filename": "error-page.png",
            "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "note": "故障时刻的截图",
        },
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["id"]
    assert attachment_id.startswith("att_")
    assert upload.json()["observed_at_known"] is True

    trigger(client, arms=("B", "C"), key="TEST-eval-c", attachment_ids=[attachment_id])
    rows = runs(client, limit=10)
    assert len(rows) == 2
    assert {row["arm"] for row in rows} == {"B", "C"}
    # 两臂共用同一份快照（含视觉证据）。
    assert len({row["snapshot_hash"] for row in rows}) == 1
    detail = client.get(f"/api/evaluation/runs/{rows[0]['id']}").json()
    items = detail["evidence"].get("items") or []
    kinds = {item["kind"] for item in items}
    assert "attachment" in kinds
    attachment = next(item for item in items if item["kind"] == "attachment")
    # 视觉观测确实进了快照：模型看到的是截图里的异常，不是空壳。
    assert attachment["raw"]["anomalies"][0]["description"] == "数据库错误页"
