import pytest
from app.agents.evidence import EvidenceAgent
from app.config import COMPOSE_PROJECT, SERVICES, Settings
from app.graph.workflow import Workflow
from app.main import create_app
from app.schemas.domain import (
    Diagnosis,
    DomainReview,
    Plan,
    Probe,
    Scenario,
    Verdict,
    Verification,
    VisionObservation,
)
from app.services.attachments import AttachmentService
from app.services.evaluation import EvaluationService
from app.services.executor import ActionExecutor
from app.services.platform import Platform
from fastapi.testclient import TestClient


class TestWorld:
    __test__ = False

    # 停止某个服务时，哪些探针会真的失败。
    FAILING_BY_SERVICE = {
        "demo-mysql": ("mysql_select_1", "app_db_check"),
        "demo-redis": ("redis_roundtrip", "app_cache_check"),
        "demo-app": ("nginx_hello", "app_db_check", "app_cache_check", "app_health"),
        "demo-nginx": ("nginx_hello",),
    }
    CHECKS_BY_SCENARIO = {
        Scenario.MYSQL_DOWN: ("mysql_select_1", "app_db_check"),
        Scenario.REDIS_DOWN: ("redis_roundtrip", "app_cache_check"),
        Scenario.NGINX_502: ("nginx_hello",),
    }
    BASELINE_PROBES = (
        "mysql_select_1",
        "app_db_check",
        "redis_roundtrip",
        "app_cache_check",
        "nginx_hello",
        "app_health",
    )

    def __init__(self):
        self.stopped_service = None
        self.operations = []
        self.verdicts = [Verdict.PASS]
        self.action = "START_DEMO_MYSQL"
        self.diagnosis_error = None
        self.missing_evidence = False
        self.diagnosis_contexts = []
        self.evidence_sufficient = True
        self.blind_contexts = []
        self.capability_checks = 0
        self.blind_error = None
        self.blind_review_error = None
        self.blind_root_cause = "MYSQL_SERVICE_STOPPED"
        self.review_verdict = "agree"
        self.review_root_cause = "MYSQL_SERVICE_STOPPED"
        self.requested_checks: list[list[dict]] = []
        self.candidates: list[dict] = []
        self.window_enabled = False
        self.window_instability = False
        # 停止操作不生效：用来模拟「注入了但故障没出现」。
        self.stop_has_no_effect = False
        self.vision_observation = VisionObservation()

    @property
    def stopped(self) -> bool:
        return self.stopped_service is not None

    @stopped.setter
    def stopped(self, value: bool) -> None:
        # 兼容旧用例：布尔语义等价于「demo-mysql 是否停止」。
        self.stopped_service = "demo-mysql" if value else None

    def snapshot(self):
        return {
            s: {
                "state": "exited" if self.stopped_service == s else "running",
                "health": "healthy",
                "project": COMPOSE_PROJECT,
            }
            for s in SERVICES
        }

    def change(self, service, operation):
        self.operations.append((service, operation))
        if operation == "stop":
            if not self.stop_has_no_effect:
                self.stopped_service = service
        elif self.stopped_service == service:
            self.stopped_service = None
        return {"exit_code": 0, "service": service, "operation": operation, "mode": "TEST/MOCK"}

    def logs(self, service):
        return f"TEST/MOCK {service} connection refused password=private"

    def probe_for(self, name):
        failing = self.FAILING_BY_SERVICE.get(self.stopped_service, ())
        verdict = (
            Verdict.UNKNOWN
            if self.missing_evidence
            else (Verdict.FAIL if name in failing else Verdict.PASS)
        )
        observed = {"mode": "TEST/MOCK"}
        if name == "nginx_hello" and self.stopped_service == "demo-app":
            observed = {"status_code": 502, "mode": "TEST/MOCK"}
        return Probe(name=name, verdict=verdict, observed=observed)

    def mysql(self):
        return [self.probe_for(name) for name in ("mysql_select_1", "app_db_check")]

    def by_name(self, name):
        return self.probe_for(name)

    def for_scenario(self, scenario):
        return [self.probe_for(name) for name in self.CHECKS_BY_SCENARIO[scenario]]

    def baseline(self):
        return [self.probe_for(name) for name in self.BASELINE_PROBES]

    def verify(self, on_sample, scenario=Scenario.MYSQL_DOWN):
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        self.stopped = verdict == Verdict.FAIL
        probes = [
            Probe(name=name, verdict=verdict, observed={"mode": "TEST/MOCK"})
            for name in ("mysql_select_1", "app_db_check")
        ]
        on_sample(probes)
        window = None
        if self.window_enabled:
            window = {
                "enabled": True,
                "required_passes": 1,
                "window_seconds": 0,
                "samples": 1,
                "failures": 1 if self.window_instability else 0,
                "consecutive_passes": 1,
                "instability": self.window_instability,
                "stable": not self.window_instability,
            }
        return Verification(verdict=verdict, samples=[probes], window=window)

    def diagnose(self, context, trace=None):
        self.diagnosis_contexts.append(context)
        if self.diagnosis_error:
            raise self.diagnosis_error
        requests = []
        if self.requested_checks:
            requests = list(self.requested_checks.pop(0))
        diagnosis = Diagnosis(
            root_cause="MYSQL_SERVICE_STOPPED",
            summary="TEST/MOCK 数据库已停止",
            evidence_ids=[e["id"] for e in context["evidence"]],
            confidence=0.9,
            evidence_sufficient=self.evidence_sufficient,
            candidates=list(self.candidates),
            requested_checks=requests,
        )
        if trace is not None:
            trace.raw = {"analysis": diagnosis.model_dump_json()}
            trace.usage = {"total_tokens": 0}
        return diagnosis

    def plan(self, context):
        return Plan(
            action_id=self.action,
            target="demo-mysql",
            reason="TEST/MOCK 恢复数据库",
            risk="LOW",
            expected_checks=["mysql_select_1", "app_db_check"],
        )

    def ensure_capabilities(self):
        self.capability_checks += 1

    def diagnose_domain(
        self, context, required_ids=None, timeout=None, trace=None, verify_capabilities=True
    ):
        self.blind_contexts.append(context)
        if self.blind_error:
            raise self.blind_error
        diagnosis = Diagnosis(
            root_cause=self.blind_root_cause,
            summary="TEST/MOCK 盲诊结论",
            evidence_ids=[e["id"] for e in context["evidence"]],
            confidence=0.8,
            evidence_sufficient=self.evidence_sufficient,
        )
        if trace is not None:
            trace.raw = {"analysis": diagnosis.model_dump_json()}
            trace.latency_ms = 1
            trace.usage = None
        return diagnosis

    def review_domain(
        self, context, required_ids=None, timeout=None, trace=None, verify_capabilities=True
    ):
        self.blind_contexts.append(context)
        if self.blind_review_error:
            raise self.blind_review_error
        if self.blind_error:
            raise self.blind_error
        review = DomainReview(
            verdict=self.review_verdict,
            root_cause=self.review_root_cause,
            reason="TEST/MOCK 复核理由",
            evidence_ids=[e["id"] for e in context["evidence"]],
            evidence_sufficient=True,
        )
        if trace is not None:
            trace.raw = {"analysis": review.model_dump_json()}
            trace.latency_ms = 1
            trace.usage = None
        return review

    def analyze(self, image_base64, mime, note=""):
        return self.vision_observation

    def configured(self):
        return True

    def factory(self, settings, store, saver, client):
        evidence = EvidenceAgent(settings, self, self)
        workflow = Workflow(store, evidence, self, self, ActionExecutor(store, self), self, saver)
        attachments = AttachmentService(settings, self)
        platform = Platform(
            settings,
            store,
            self,
            self,
            evidence,
            workflow,
            client,
            mode="TEST/MOCK",
            attachments=attachments,
        )
        evaluation = EvaluationService(settings, store, self, self, evidence, self)
        return platform, evaluation


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
