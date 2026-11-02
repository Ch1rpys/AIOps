import pytest
from app.agents.evidence import EvidenceAgent
from app.config import COMPOSE_PROJECT, SERVICES, Settings
from app.graph.workflow import Workflow
from app.main import create_app
from app.schemas.domain import Diagnosis, Plan, Probe, Scenario, Verdict, Verification
from app.services.executor import ActionExecutor
from app.services.platform import Platform
from fastapi.testclient import TestClient


class TestWorld:
    __test__ = False

    def __init__(self):
        self.stopped = False
        self.operations = []
        self.verdicts = [Verdict.PASS]
        self.action = "START_DEMO_MYSQL"
        self.diagnosis_error = None
        self.missing_evidence = False
        self.diagnosis_contexts = []
        self.evidence_sufficient = True

    def snapshot(self):
        return {
            s: {
                "state": "exited" if self.stopped and s == "demo-mysql" else "running",
                "health": "healthy",
                "project": COMPOSE_PROJECT,
            }
            for s in SERVICES
        }

    def change(self, service, operation):
        self.operations.append((service, operation))
        if service == "demo-mysql":
            self.stopped = operation == "stop"
        return {"exit_code": 0, "service": service, "operation": operation, "mode": "TEST/MOCK"}

    def logs(self, service):
        return f"TEST/MOCK {service} connection refused password=private"

    def mysql(self):
        verdict = (
            Verdict.UNKNOWN
            if self.missing_evidence
            else (Verdict.FAIL if self.stopped else Verdict.PASS)
        )
        return [
            Probe(name=name, verdict=verdict, observed={"mode": "TEST/MOCK"})
            for name in ("mysql_select_1", "app_db_check")
        ]

    def for_scenario(self, scenario):
        return self.mysql()

    def baseline(self):
        verdict = (
            Verdict.UNKNOWN
            if self.missing_evidence
            else (Verdict.FAIL if self.stopped else Verdict.PASS)
        )
        return [
            Probe(name=name, verdict=verdict, observed={"mode": "TEST/MOCK"})
            for name in (
                "mysql_select_1",
                "app_db_check",
                "redis_roundtrip",
                "app_cache_check",
                "nginx_hello",
                "app_health",
            )
        ]

    def verify(self, on_sample, scenario=Scenario.MYSQL_DOWN):
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        self.stopped = verdict == Verdict.FAIL
        probes = [
            Probe(name=name, verdict=verdict, observed={"mode": "TEST/MOCK"})
            for name in ("mysql_select_1", "app_db_check")
        ]
        on_sample(probes)
        return Verification(verdict=verdict, samples=[probes])

    def diagnose(self, context):
        self.diagnosis_contexts.append(context)
        if self.diagnosis_error:
            raise self.diagnosis_error
        return Diagnosis(
            root_cause="MYSQL_SERVICE_STOPPED",
            summary="TEST/MOCK 数据库已停止",
            evidence_ids=[e["id"] for e in context["evidence"]],
            confidence=0.9,
            evidence_sufficient=self.evidence_sufficient,
        )

    def plan(self, context):
        return Plan(
            action_id=self.action,
            target="demo-mysql",
            reason="TEST/MOCK 恢复数据库",
            risk="LOW",
            expected_checks=["mysql_select_1", "app_db_check"],
        )

    def factory(self, settings, store, saver, client):
        evidence = EvidenceAgent(settings, self, self)
        workflow = Workflow(store, evidence, self, self, ActionExecutor(store, self), self, saver)
        return Platform(settings, store, self, self, evidence, workflow, client, mode="TEST/MOCK")


@pytest.fixture
def world():
    return TestWorld()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        admin_token="TEST-admin-token",
        demo_mysql_password="private",
        verification_interval=0,
        fault_confirmation_interval=0,
    )


@pytest.fixture
def client(settings, world):
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        yield client


def injected(client):
    response = client.post(
        "/api/scenarios/MYSQL_DOWN/inject",
        json={"confirm": True},
        headers={"Idempotency-Key": "TEST-inject-0001"},
    )
    assert response.status_code == 201, response.text
    return response.json()["incident_id"]


def waiting(client):
    incident_id = injected(client)
    response = client.post(f"/api/incidents/{incident_id}/diagnose")
    assert response.status_code == 202, response.text
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "WAITING_APPROVAL", item
    return incident_id, item["detail"]["plan_id"]


def approve(client, incident_id, plan_id, decision="approve", key="TEST-approve-0001"):
    return client.post(
        f"/api/incidents/{incident_id}/approval",
        json={"plan_id": plan_id, "decision": decision, "comment": "TEST/MOCK"},
        headers={"Idempotency-Key": key},
    )
