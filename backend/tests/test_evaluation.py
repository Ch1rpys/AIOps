"""盲诊评测的护栏（docs/落地执行计划.md §4.6）。

重点不是「跑通」，而是三件事：
真值不外泄、A/B 在同一份快照上可比、失败与拒答不被美化。
"""

import pytest
from app.repositories.store import EvaluationRun
from app.schemas.domain import now


def trigger(client, scenario="MYSQL_DOWN", arms=("A", "B"), key="TEST-eval-0001"):
    response = client.post(
        "/api/evaluation/runs",
        json={"scenario": scenario, "arms": list(arms), "confirm": True},
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
