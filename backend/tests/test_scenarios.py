import httpx
import pytest
import redis
from app.agents.evidence import fault_observed
from app.config import DomainError
from app.integrations.probes import Probes, judge
from app.main import create_app
from app.scenarios import SCENARIOS
from app.schemas.domain import Diagnosis, Evidence, Plan, Probe, Scenario, Verdict, Verification
from app.services.executor import validate_plan
from conftest import TestWorld, approve
from fastapi.testclient import TestClient


class ScenarioWorld(TestWorld):
    def __init__(self):
        super().__init__()
        self.stopped_services = set()

    def snapshot(self):
        states = super().snapshot()
        for service in self.stopped_services:
            states[service]["state"] = "exited"
        return states

    def change(self, service, operation):
        self.operations.append((service, operation))
        if operation == "stop":
            self.stopped_services.add(service)
        else:
            self.stopped_services.discard(service)
        return {"mode": "TEST/MOCK", "exit_code": 0, "service": service, "operation": operation}

    def for_scenario(self, scenario):
        definition = SCENARIOS[scenario]
        failed = definition.target in self.stopped_services
        return [
            Probe(
                name=name,
                verdict=Verdict.FAIL if failed else Verdict.PASS,
                observed={"mode": "TEST/MOCK", "status_code": 502 if failed else 200},
            )
            for name in definition.checks
        ]

    def baseline(self):
        return [p for scenario in Scenario for p in self.for_scenario(scenario)]

    def diagnose(self, context):
        self.diagnosis_contexts.append(context)
        definition = SCENARIOS[Scenario(context["scenario_key"])]
        return Diagnosis(
            root_cause=definition.root_cause,
            summary="TEST/MOCK",
            evidence_ids=[e["id"] for e in context["evidence"]],
            confidence=0.9,
            evidence_sufficient=True,
            candidates=[
                {
                    "root_cause": definition.root_cause,
                    "support_evidence_ids": [
                        e["id"] for e in context["evidence"] if e.get("kind") == "container_state"
                    ],
                    "against_evidence_ids": [],
                    "missing_checks": ["app_health"],
                },
                {
                    "root_cause": "OTHER",
                    "support_evidence_ids": [],
                    "against_evidence_ids": [
                        e["id"] for e in context["evidence"] if e.get("kind") == "probe"
                    ],
                    "missing_checks": ["app_health"],
                },
            ],
        )

    def plan(self, context):
        definition = SCENARIOS[Scenario(context["scenario_key"])]
        return Plan(
            action_id=definition.action,
            target=definition.target,
            reason="TEST/MOCK",
            risk="LOW",
            expected_checks=list(definition.checks),
        )

    def verify(self, on_sample, scenario=Scenario.MYSQL_DOWN):
        sample = self.for_scenario(scenario)
        on_sample(sample)
        return Verification(verdict=judge(sample, scenario), samples=[sample])


@pytest.mark.parametrize("scenario", list(Scenario))
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_scenario_graph_end_to_end(settings, scenario, decision):
    world = ScenarioWorld()
    definition = SCENARIOS[scenario]
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        result = client.post(
            f"/api/scenarios/{scenario}/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": "TEST-multi-injection"},
        )
        assert result.status_code == 201, result.text
        incident_id = result.json()["incident_id"]
        client.post(f"/api/incidents/{incident_id}/diagnose")
        item = client.get(f"/api/incidents/{incident_id}").json()
        assert item["status"] == "WAITING_APPROVAL", item
        assert item["detail"]["plan"]["action_id"] == definition.action
        assert world.diagnosis_contexts[-1]["scenario_key"] == scenario
        plan_id = item["detail"]["plan_id"]
        approve(client, incident_id, plan_id, decision)
        approve(client, incident_id, plan_id, decision)
        item = client.get(f"/api/incidents/{incident_id}").json()
        assert item["status"] == ("RESOLVED" if decision == "approve" else "CANCELLED")
        assert world.operations == [(definition.target, "stop")] + (
            [(definition.target, "start")] if decision == "approve" else []
        )
        if decision == "approve":
            assert {p["name"] for p in item["detail"]["verification"]["samples"][-1]} == set(
                definition.checks
            )
        assert any(
            r["kind"] == "evidence" and r["payload"]["source"].endswith(definition.runbook + ".md")
            for r in item["records"]
        )


@pytest.mark.parametrize("scenario", list(Scenario))
@pytest.mark.parametrize("action_scenario", list(Scenario))
def test_action_cannot_cross_scenarios(scenario, action_scenario):
    action = SCENARIOS[action_scenario]
    plan = Plan(
        action_id=action.action,
        target=action.target,
        reason="TEST/MOCK",
        risk="LOW",
        expected_checks=list(action.checks),
    )
    if scenario == action_scenario:
        validate_plan(plan, scenario)
    else:
        with pytest.raises(DomainError):
            validate_plan(plan, scenario)


@pytest.mark.parametrize("status,confirmed", [(502, True), (500, False), (404, False)])
def test_nginx_injection_requires_actual_502(status, confirmed):
    evidence = [
        Evidence(source="docker", kind="container_state", raw={"demo-app": {"state": "exited"}}),
        Evidence(
            source="nginx_hello",
            kind="probe",
            raw=Probe(
                name="nginx_hello", verdict=Verdict.FAIL, observed={"status_code": status}
            ).model_dump(mode="json"),
        ),
    ]
    assert fault_observed(evidence, Scenario.NGINX_502) is confirmed


@pytest.mark.parametrize("scenario", [Scenario.REDIS_DOWN, Scenario.NGINX_502])
def test_verdict_does_not_accept_mysql_checks_for_other_scenarios(scenario):
    probes = [
        Probe(name=n, verdict=Verdict.PASS, observed={})
        for n in SCENARIOS[Scenario.MYSQL_DOWN].checks
    ]
    assert judge(probes, scenario) == Verdict.UNKNOWN


@pytest.mark.parametrize(
    "failure,expected",
    [
        (redis.ConnectionError, Verdict.FAIL),
        (redis.TimeoutError, Verdict.UNKNOWN),
        (redis.AuthenticationError, Verdict.UNKNOWN),
    ],
)
def test_redis_failure_classification(settings, monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise failure("TEST/MOCK")

    monkeypatch.setattr(redis.Redis, "ping", fail)
    with httpx.Client() as client:
        assert Probes(settings, client).redis_roundtrip().verdict == expected


@pytest.mark.parametrize("scenario", [Scenario.REDIS_DOWN, Scenario.NGINX_502])
def test_new_scenario_approval_checkpoint_survives_restart(settings, scenario):
    world = ScenarioWorld()
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        result = client.post(
            f"/api/scenarios/{scenario}/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": "TEST-checkpoint-inject"},
        )
        incident_id = result.json()["incident_id"]
        client.post(f"/api/incidents/{incident_id}/diagnose")
        plan_id = client.get(f"/api/incidents/{incident_id}").json()["detail"]["plan_id"]
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert approve(client, incident_id, plan_id).status_code == 202
        item = client.get(f"/api/incidents/{incident_id}").json()
        assert item["status"] == "RESOLVED"
        assert item["scenario_key"] == scenario
        assert world.operations == [
            (SCENARIOS[scenario].target, "stop"),
            (SCENARIOS[scenario].target, "start"),
        ]


@pytest.mark.parametrize(
    "body,expected",
    [({"message": "hello from demo-app"}, Verdict.PASS), ({"message": "other"}, Verdict.FAIL)],
)
def test_nginx_verification_uses_proxy_and_exact_body(settings, body, expected):
    def handler(request):
        assert str(request.url) == settings.nginx_url + "/api/hello"
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        probes = Probes(settings, client).for_scenario(Scenario.NGINX_502)
        assert judge(probes, Scenario.NGINX_502) == expected


def test_redis_probe_runs_ping_set_get(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(redis.Redis, "ping", lambda self: calls.append("PING") or True)

    def set_value(self, key, value, ex):
        calls.append(("SET", key, value, ex))
        return True

    monkeypatch.setattr(redis.Redis, "set", set_value)
    monkeypatch.setattr(
        redis.Redis, "get", lambda self, key: calls.append(("GET", key)) or b"probe"
    )
    with httpx.Client() as client:
        assert Probes(settings, client).redis_roundtrip().verdict == Verdict.PASS
    assert calls == ["PING", ("SET", "aiops:health", "probe", 30), ("GET", "aiops:health")]


@pytest.mark.parametrize("scenario", [Scenario.REDIS_DOWN, Scenario.NGINX_502])
@pytest.mark.parametrize("verdict", [Verdict.FAIL, Verdict.UNKNOWN])
def test_new_scenarios_retry_limit_and_unknown(settings, scenario, verdict):
    world = ScenarioWorld()

    def failed_verification(on_sample, selected):
        assert selected == scenario
        world.stopped_services.add(SCENARIOS[selected].target)
        sample = [
            Probe(name=name, verdict=verdict, observed={"mode": "TEST/MOCK"})
            for name in SCENARIOS[selected].checks
        ]
        on_sample(sample)
        return Verification(verdict=verdict, samples=[sample])

    world.verify = failed_verification
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        result = client.post(
            f"/api/scenarios/{scenario}/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": "TEST-loop-injection"},
        )
        incident_id = result.json()["incident_id"]
        client.post(f"/api/incidents/{incident_id}/diagnose")
        first = client.get(f"/api/incidents/{incident_id}").json()
        approve(client, incident_id, first["detail"]["plan_id"])
        item = client.get(f"/api/incidents/{incident_id}").json()
        if verdict == Verdict.FAIL:
            assert item["status"] == "WAITING_APPROVAL"
            assert item["detail"]["attempt"] == 2
            assert item["detail"]["plan_id"] != first["detail"]["plan_id"]
            approve(client, incident_id, item["detail"]["plan_id"], key="TEST-loop-second-approval")
            item = client.get(f"/api/incidents/{incident_id}").json()
            assert item["status"] == "FAILED"
            assert len(world.diagnosis_contexts) == 2
            assert world.diagnosis_contexts[-1]["previous_execution"]["state"] == "SUCCEEDED"
        else:
            assert item["status"] == "UNKNOWN"
            assert len(world.diagnosis_contexts) == 1
        assert world.operations.count((SCENARIOS[scenario].target, "start")) == 1


@pytest.mark.parametrize("eventual_502", [True, False])
def test_nginx_injection_preserves_transient_504_then_confirms_502(
    settings, monkeypatch, eventual_502
):
    world = ScenarioWorld()
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        agent = client.app.state.platform.evidence
        original = agent.collect
        count = 0

        def delayed_proxy(scenario):
            nonlocal count
            count += 1
            items = original(scenario)
            if count == 1 or not eventual_502:
                for entry in items:
                    if entry.kind == "probe":
                        entry.raw["observed"]["status_code"] = 504
            return items

        monkeypatch.setattr(agent, "collect", delayed_proxy)
        result = client.post(
            "/api/scenarios/NGINX_502/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": "TEST-transient-504"},
        )
        assert result.status_code == (201 if eventual_502 else 502), result.text
        incident_id = (
            result.json()["incident_id"]
            if eventual_502
            else client.get("/api/incidents").json()[0]["id"]
        )
        item = client.get("/api/incidents/" + incident_id).json()
        observations = [
            r["payload"]["raw"]["observed"]["status_code"]
            for r in item["records"]
            if r["kind"] == "evidence" and r["payload"]["kind"] == "probe"
        ]
        assert observations == (
            [504, 502] if eventual_502 else [504] * settings.fault_confirmation_attempts
        )
        assert count == (2 if eventual_502 else settings.fault_confirmation_attempts)
        assert item["detail"]["fault_confirmed"] is eventual_502
        assert world.operations == [("demo-app", "stop")]


def test_semantically_rejected_diagnosis_remains_auditable(settings):
    world = ScenarioWorld()
    original = world.diagnose

    def uncertain(context):
        result = original(context)
        result.evidence_sufficient = False
        result.summary = "TEST/MOCK evidence insufficient"
        return result

    world.diagnose = uncertain
    with TestClient(create_app(settings, world.factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        created = client.post(
            "/api/scenarios/NGINX_502/inject",
            json={"confirm": True},
            headers={"Idempotency-Key": "TEST-uncertain-model"},
        ).json()
        client.post(f"/api/incidents/{created['incident_id']}/diagnose")
        item = client.get(f"/api/incidents/{created['incident_id']}").json()
        assert item["status"] == "UNKNOWN"
        assert item["detail"]["diagnosis"]["summary"] == "TEST/MOCK evidence insufficient"
        assert world.operations == [("demo-app", "stop")]
