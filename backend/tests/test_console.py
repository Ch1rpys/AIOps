import pytest
from app.config import DomainError
from app.main import create_app
from app.repositories.store import Incident
from app.schemas.domain import Scenario, Status, now
from fastapi.testclient import TestClient


def test_all_scenarios_expose_matching_targets_and_checks(client):
    items = client.get("/api/scenarios").json()
    assert {item["key"] for item in items} == set(Scenario)
    targets = {
        "MYSQL_DOWN": ("demo-mysql", ["mysql_select_1", "app_db_check"]),
        "REDIS_DOWN": ("demo-redis", ["redis_roundtrip", "app_cache_check"]),
        "NGINX_502": ("demo-app", ["nginx_hello"]),
        "NGINX_UPSTREAM": ("demo-nginx", ["nginx_hello"]),
        "APP_HTTP_500": ("demo-app", ["nginx_hello"]),
    }
    for item in items:
        assert item["enabled"] is True
        assert (item["target"], item["checks"]) == targets[item["key"]]


def test_history_filters_before_pagination_and_exposes_saved_results(client):
    store = client.app.state.platform.store
    with store.session() as session, session.begin():
        for index, (scenario, status) in enumerate(
            [
                (Scenario.MYSQL_DOWN, Status.RESOLVED),
                (Scenario.REDIS_DOWN, Status.UNKNOWN),
                (Scenario.REDIS_DOWN, Status.RESOLVED),
                (Scenario.REDIS_DOWN, Status.RESOLVED),
            ]
        ):
            session.add(
                Incident(
                    id=f"TEST-history-{index}",
                    scenario_key=scenario,
                    status=status,
                    mode="TEST/MOCK",
                    created_at=f"2026-10-31T08:00:0{index}+00:00",
                    updated_at=now(),
                    finished_at=now(),
                    detail={
                        "diagnosis": {"root_cause": "REDIS_SERVICE_STOPPED"},
                        "verification": {"verdict": "PASS" if status == "RESOLVED" else "UNKNOWN"},
                    },
                )
            )
    response = client.get(
        "/api/incidents", params={"scenario": "REDIS_DOWN", "status": "RESOLVED", "limit": 1}
    )
    assert response.status_code == 200
    item = response.json()[0]
    assert item["id"] == "TEST-history-3"
    assert item["root_cause"] == "REDIS_SERVICE_STOPPED"
    assert item["verification_verdict"] == "PASS"
    assert item["finished_at"]
    second = client.get(
        "/api/incidents",
        params={"scenario": "REDIS_DOWN", "status": "RESOLVED", "limit": 1, "offset": 1},
    ).json()
    assert [item["id"] for item in second] == ["TEST-history-2"]
    assert client.get("/api/incidents?scenario=UNREGISTERED").status_code == 422


def test_history_without_observations_never_invents_verdict(client):
    created = client.post(
        "/api/scenarios/MYSQL_DOWN/inject",
        json={"confirm": True},
        headers={"Idempotency-Key": "TEST-console-inject"},
    )
    assert created.status_code == 201
    item = client.get("/api/incidents").json()[0]
    assert item["root_cause"] is None
    assert item["verification_verdict"] is None
    assert item["finished_at"] is None


@pytest.mark.parametrize("failure", [None, "DOCKER_UNAVAILABLE"])
def test_environment_preserves_unknown_observations(settings, world, failure):
    world.missing_evidence = True
    if failure:

        def unavailable():
            raise DomainError(failure, "TEST/MOCK Docker unavailable", 503)

        world.snapshot = unavailable
    with TestClient(create_app(settings, world.factory)) as client:
        result = client.get("/api/lab/status").json()
        assert result["healthy"] is False
        assert result["compose_project"] == "aiops-demo"
        assert {probe["verdict"] for probe in result["probes"]} == {"UNKNOWN"}
        if failure:
            assert result["services"] == {}
            assert result["error"]["code"] == failure
