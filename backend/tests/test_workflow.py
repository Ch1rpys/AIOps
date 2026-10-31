from concurrent.futures import ThreadPoolExecutor

import pytest
from app.config import DomainError
from app.main import create_app
from app.schemas.domain import ApprovalRequest, Plan, Probe, Verdict
from app.services.executor import ActionExecutor
from conftest import approve, injected, waiting
from fastapi.testclient import TestClient
from filelock import Timeout


def detail(client, incident_id):
    return client.get(f"/api/incidents/{incident_id}").json()


def test_approved_roundtrip_and_report(client, world):
    incident_id, plan_id = waiting(client)
    assert world.operations == [("demo-mysql", "stop")]
    assert approve(client, incident_id, plan_id).status_code == 202
    item = detail(client, incident_id)
    assert item["status"] == "RESOLVED"
    assert item["mode"] == "TEST/MOCK"
    assert item["detail"]["verification"]["verdict"] == "PASS"
    assert world.operations == [("demo-mysql", "stop"), ("demo-mysql", "start")]
    kinds = {r["kind"] for r in item["records"]}
    assert {"evidence", "step", "plan", "approval", "execution", "verification"} <= kinds
    assert "private" not in str(item)
    report = client.get(f"/api/incidents/{incident_id}/report.md")
    assert "TEST/MOCK" in report.text and "mysql_select_1" in report.text


def test_rejection_never_executes_and_keeps_lab_reserved(client, world):
    incident_id, plan_id = waiting(client)
    assert approve(client, incident_id, plan_id, "reject").status_code == 202
    assert detail(client, incident_id)["status"] == "CANCELLED"
    assert world.operations == [("demo-mysql", "stop")]
    response = client.post(
        "/api/scenarios/MYSQL_DOWN/inject",
        json={"confirm": True},
        headers={"Idempotency-Key": "TEST-another-0001"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAB_BUSY"
    assert (
        client.post(
            "/api/lab/reset", json={"confirm": True}, headers={"Idempotency-Key": "TEST-reset-0001"}
        ).status_code
        == 200
    )


def test_idempotency_injection_and_approval(client, world):
    incident_id, plan_id = waiting(client)
    repeated = client.post(
        "/api/scenarios/MYSQL_DOWN/inject",
        json={"confirm": True},
        headers={"Idempotency-Key": "TEST-inject-0001"},
    )
    assert repeated.json()["incident_id"] == incident_id
    first = approve(client, incident_id, plan_id)
    second = approve(client, incident_id, plan_id)
    assert first.json() == second.json()
    assert world.operations.count(("demo-mysql", "start")) == 1
    assert approve(client, incident_id, plan_id, "reject").status_code == 409
    assert approve(client, incident_id, plan_id, key="TEST-approve-0002").status_code == 409


@pytest.mark.parametrize(
    "verdict,status", [(Verdict.UNKNOWN, "UNKNOWN"), (Verdict.PASS, "RESOLVED")]
)
def test_probe_verdict_controls_final_state(client, world, verdict, status):
    world.verdicts = [verdict]
    incident_id, plan_id = waiting(client)
    approve(client, incident_id, plan_id)
    assert detail(client, incident_id)["status"] == status


def test_two_round_limit_requires_fresh_approval(client, world):
    world.verdicts = [Verdict.FAIL, Verdict.FAIL]
    incident_id, first_plan = waiting(client)
    approve(client, incident_id, first_plan)
    item = detail(client, incident_id)
    assert item["status"] == "WAITING_APPROVAL"
    assert item["detail"]["attempt"] == 2
    second_plan = item["detail"]["plan_id"]
    assert second_plan != first_plan
    assert approve(client, incident_id, first_plan, key="TEST-stale-plan").status_code == 409
    approve(client, incident_id, second_plan, key="TEST-approve-0002")
    item = detail(client, incident_id)
    assert item["status"] == "FAILED"
    assert len([r for r in item["records"] if r["kind"] == "verification"]) == 2
    assert world.operations.count(("demo-mysql", "start")) == 1
    assert world.diagnosis_contexts[-1]["previous_verification"]["verdict"] == "FAIL"
    assert world.diagnosis_contexts[-1]["previous_execution"]["state"] == "SUCCEEDED"


def test_cross_scenario_action_is_blocked(client, world):
    world.action = "START_DEMO_REDIS"
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    assert detail(client, incident_id)["status"] == "BLOCKED"
    assert world.operations == [("demo-mysql", "stop")]


def test_ai_unavailable_visible_no_fallback(client, world):
    world.diagnosis_error = DomainError("AI_UNAVAILABLE", "TEST/MOCK offline", 503)
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = detail(client, incident_id)
    assert item["status"] == "ERROR"
    assert item["detail"]["error"]["code"] == "AI_UNAVAILABLE"
    assert world.operations == [("demo-mysql", "stop")]


def test_checkpoint_survives_new_app_instance(settings, world):
    app = create_app(settings, world.factory)
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        incident_id, plan_id = waiting(client)
    with TestClient(create_app(settings, world.factory)) as reopened:
        reopened.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert detail(reopened, incident_id)["status"] == "WAITING_APPROVAL"
        assert approve(reopened, incident_id, plan_id).status_code == 202
        assert detail(reopened, incident_id)["status"] == "RESOLVED"
        assert world.operations.count(("demo-mysql", "start")) == 1


def test_missing_evidence_does_not_claim_injection(client, world):
    world.missing_evidence = True
    assert (
        client.post(
            "/api/scenarios/MYSQL_DOWN/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": "TEST-inject-0001"},
        ).status_code
        == 409
    )


def test_auth_origin_and_confirmation(client):
    client.headers.pop("X-Admin-Token")
    assert (
        client.post(
            "/api/lab/reset", json={"confirm": True}, headers={"Idempotency-Key": "TEST-reset-0001"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/lab/reset", json={"confirm": True}, headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )


def test_stale_or_forged_approval_before_diagnosis(client, world):
    incident_id = injected(client)
    assert approve(client, incident_id, "plan-forged").status_code == 409
    assert world.operations == [("demo-mysql", "stop")]


def test_reset_receipt_does_not_replay_writes(client, world):
    incident_id = injected(client)
    kwargs = {"json": {"confirm": True}, "headers": {"Idempotency-Key": "TEST-reset-0001"}}
    first = client.post("/api/lab/reset", **kwargs)
    second = client.post("/api/lab/reset", **kwargs)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert detail(client, incident_id)["status"] == "CANCELLED"
    assert len(world.operations) == 5


@pytest.mark.parametrize("confirmation", [False, 1, "true", None])
def test_confirmation_is_explicit_boolean(client, world, confirmation):
    response = client.post(
        "/api/scenarios/MYSQL_DOWN/inject",
        json={"confirm": confirmation},
        headers={"Idempotency-Key": "TEST-invalid-confirm"},
    )
    assert response.status_code == 422
    assert world.operations == []


def test_concurrent_injections_only_stop_once(client, world):
    def inject_one(index):
        return client.post(
            "/api/scenarios/MYSQL_DOWN/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": f"TEST-concurrent-{index}"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(inject_one, [1, 2]))
    assert sorted(statuses) == [201, 409]
    assert world.operations == [("demo-mysql", "stop")]


def test_uncertain_execution_never_replays(client, world):
    incident_id, plan_id = waiting(client)
    service = client.app.state.platform
    request = ApprovalRequest(plan_id=plan_id, decision="approve")
    service.approve(incident_id, request, "TEST-persisted-approval")
    with service.store.session() as session, session.begin():
        service.store.audit(
            session,
            incident_id,
            "execution",
            {"plan_id": plan_id, "action_id": "START_DEMO_MYSQL", "state": "STARTED"},
        )
    plan = Plan.model_validate(detail(client, incident_id)["detail"]["plan"])
    with pytest.raises(DomainError) as error:
        ActionExecutor(service.store, world).execute(incident_id, plan_id, plan)
    assert error.value.code == "EXECUTION_UNCERTAIN"
    assert world.operations == [("demo-mysql", "stop")]


def test_interrupted_run_visible_after_restart(settings, world):
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        incident_id, plan_id = waiting(client)
        client.app.state.platform.approve(
            incident_id,
            ApprovalRequest(plan_id=plan_id, decision="approve"),
            "TEST-crashed-approval",
        )
    with TestClient(create_app(settings, world.factory)) as reopened:
        item = detail(reopened, incident_id)
        assert item["status"] == "ERROR"
        assert item["detail"]["error"]["code"] == "PROCESS_INTERRUPTED"
        assert world.operations == [("demo-mysql", "stop")]


def test_second_worker_is_rejected(client, settings, world):
    with pytest.raises(Timeout), TestClient(create_app(settings, world.factory)):
        pytest.fail("第二个 worker 不应取得实验锁")


def test_injection_unexpected_failure_is_durable_and_not_replayed(client, world, monkeypatch):
    def failed_change(*args):
        raise RuntimeError("TEST/MOCK untrusted failure")

    monkeypatch.setattr(world, "change", failed_change)
    kwargs = {"json": {"confirm": True}, "headers": {"Idempotency-Key": "TEST-failed-injection"}}
    first = client.post("/api/scenarios/MYSQL_DOWN/inject", **kwargs)
    second = client.post("/api/scenarios/MYSQL_DOWN/inject", **kwargs)
    assert first.status_code == second.status_code == 500
    assert first.json() == second.json()
    item = client.get("/api/incidents").json()[0]
    assert item["status"] == "ERROR"
    assert detail(client, item["id"])["detail"]["fault_confirmed"] is False


def test_probe_program_failure_is_a_failed_step_not_pass(client, world, monkeypatch):
    incident_id, plan_id = waiting(client)

    def failed_probe(*args):
        raise RuntimeError("TEST/MOCK probe bug")

    monkeypatch.setattr(world, "verify", failed_probe)
    approve(client, incident_id, plan_id)
    item = detail(client, incident_id)
    assert item["status"] == "UNKNOWN"
    failed = [
        r for r in item["records"] if r["kind"] == "step" and r["payload"]["phase"] == "VERIFYING"
    ]
    assert failed[-1]["payload"]["status"] == "FAILED"
    assert item["detail"]["verification"]["verdict"] == "UNKNOWN"
    assert item["detail"]["verification"]["samples"] == []


def test_docker_failure_records_exit_status(client, world, monkeypatch):
    incident_id, plan_id = waiting(client)

    def failed_change(*args):
        raise DomainError("DOCKER_COMMAND_FAILED", "TEST/MOCK", 503, {"exit_code": 27})

    monkeypatch.setattr(world, "change", failed_change)
    approve(client, incident_id, plan_id)
    item = detail(client, incident_id)
    assert item["status"] == "ERROR"
    executions = [r["payload"] for r in item["records"] if r["kind"] == "execution"]
    assert executions[-1]["state"] == "FAILED"
    assert executions[-1]["output"]["exit_code"] == 27


def test_retry_diagnoses_running_container_without_blind_second_start(client, world, monkeypatch):
    incident_id, plan_id = waiting(client)
    original_verify = world.verify
    world.verdicts = [Verdict.FAIL]

    def running_but_unhealthy(on_sample, scenario=None):
        result = original_verify(on_sample)
        world.stopped = False
        monkeypatch.setattr(
            world,
            "mysql",
            lambda: [
                Probe(name=name, verdict=Verdict.FAIL, observed={"mode": "TEST/MOCK"})
                for name in ("mysql_select_1", "app_db_check")
            ],
        )
        return result

    monkeypatch.setattr(world, "verify", running_but_unhealthy)
    approve(client, incident_id, plan_id)
    item = detail(client, incident_id)
    assert len(world.diagnosis_contexts) == 2
    assert item["status"] == "UNKNOWN"
    assert item["detail"]["error"]["code"] == "EVIDENCE_INSUFFICIENT"
    assert world.operations.count(("demo-mysql", "start")) == 1
