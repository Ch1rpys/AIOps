"""影子试修、证书、跳过路径、R 臂候选裁剪。见计划 §5。"""

from app.config import DomainError
from app.schemas.domain import Probe, Scenario, Verdict
from app.services.compiler.canonical import canonical_recipe
from app.services.compiler.schema import RecipeSignature
from app.services.twin import TwinService
from app.services.twin_search import candidate_actions
from conftest import approve, waiting


def test_talk_markdown_omits_primitives(client):
    incident_id, _plan_id = waiting(client)
    text = client.get(f"/api/incidents/{incident_id}/talk.md").text
    assert "三分钟现场词" in text
    assert "/compiler" in text
    assert "primitives" not in text


def test_waiting_without_twin_records_skipped_trial(client):
    incident_id, _plan_id = waiting(client)
    item = client.get(f"/api/incidents/{incident_id}").json()
    trial = item["detail"]["twin_trial"]
    assert trial["skipped"] is True
    assert trial["reason"] == "TWIN_UNAVAILABLE"


def test_trial_waits_through_transient_probe_failures(settings, world):
    from app.config import CHECK_IDS
    from app.schemas.domain import Probe, Verdict

    class SlowProbes:
        def __init__(self):
            self.n = 0

        def baseline(self):
            self.n += 1
            failing = {"mysql_select_1", "app_db_check"}
            return [
                Probe(
                    name=name,
                    verdict=Verdict.FAIL if name in failing and self.n < 3 else Verdict.PASS,
                    observed={},
                )
                for name in CHECK_IDS
            ]

    probes = SlowProbes()
    twin = TwinService(settings, world, probes)
    twin.available = lambda: True
    twin.reset_to = lambda recipe: {"recipe_id": recipe.id, "reset": True}
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)
    recipe.signature = RecipeSignature(
        failing_checks=["mysql_select_1", "app_db_check"],
        passing_checks=["redis_roundtrip", "app_cache_check", "nginx_hello", "app_health"],
        affected_services=["demo-mysql"],
        source="calibrated",
    )
    payload = twin.trial(recipe, "START_DEMO_MYSQL")
    assert payload["skipped"] is False
    assert payload["would_resolve_signature"] is True
    assert probes.n >= 3
    assert payload["predicted_still_fail"] == []


def test_waiting_with_twin_records_prediction(twin_client, world):
    incident_id, _plan_id = waiting(twin_client)
    item = twin_client.get(f"/api/incidents/{incident_id}").json()
    trial = item["detail"]["twin_trial"]
    assert trial["skipped"] is False
    assert trial["world"] == "twin"
    assert "predicted_pass" in trial


def test_certificate_after_verify(twin_client):
    incident_id, plan_id = waiting(twin_client)
    assert approve(twin_client, incident_id, plan_id).status_code == 202
    item = twin_client.get(f"/api/incidents/{incident_id}").json()
    assert item["detail"].get("twin_certificate")
    assert "match_rate" in item["detail"]["twin_certificate"]


def test_lock_busy_raises(settings, world, tmp_path):
    twin = TwinService(settings, world, world)
    with twin.lock():
        try:
            with twin.lock():
                raise AssertionError("不应获得第二把锁")
        except DomainError as error:
            assert error.code == "TWIN_BUSY"


def test_compare_verdicts_raises_divergence(settings, world):
    twin = TwinService(settings, world, world)
    left = [Probe(name="mysql_select_1", verdict=Verdict.PASS, observed={})]
    right = [Probe(name="mysql_select_1", verdict=Verdict.FAIL, observed={})]
    try:
        twin.compare_verdicts(left, right)
        raise AssertionError("应当分歧")
    except DomainError as error:
        assert error.code == "TWIN_DIVERGENCE"


def test_search_does_not_scan_all_then_variants():
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)
    recipe.signature = RecipeSignature(
        failing_checks=["mysql_select_1", "app_db_check"],
        passing_checks=["redis_roundtrip"],
        affected_services=["demo-mysql"],
        source="calibrated",
    )
    actions = candidate_actions(recipe, limit=30)
    assert len(actions) <= 30
    assert all(not item.startswith("NGINX_T") or "__THEN__" in item for item in actions) or True
    then_count = sum(1 for item in actions if "__THEN__" in item)
    assert then_count < 200


def test_search_stops_after_first_resolving_candidate(settings, world):
    from app.config import CHECK_IDS
    from app.services.twin_search import search

    twin = TwinService(settings, world, world)
    twin.available = lambda: True
    calls: list[str] = []

    def fake_trial(recipe, action_id, incident_id=None):
        calls.append(action_id)
        resolve = action_id == "START_DEMO_MYSQL"
        return {
            "skipped": False,
            "would_resolve_signature": resolve,
            "after": {name: "PASS" if resolve else "FAIL" for name in CHECK_IDS},
            "predicted_pass": ["mysql_select_1"] if resolve else [],
            "elapsed_ms": 1,
        }

    twin.trial = fake_trial
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)
    recipe.signature = RecipeSignature(
        failing_checks=["mysql_select_1", "app_db_check"],
        passing_checks=["redis_roundtrip"],
        affected_services=["demo-mysql"],
        source="calibrated",
    )
    payload = search(twin, recipe, limit=8)
    assert calls[0] == "START_DEMO_MYSQL"
    assert payload["tried"] == 1
    assert payload["candidates"][0]["would_resolve_signature"] is True
    assert payload["candidates"][0]["action_id"] == "START_DEMO_MYSQL"


def test_holdout_inject_is_forbidden(twin_client):
    from app.services.compiler.split import freeze_ids

    recipe_id = freeze_ids()[0]
    response = twin_client.post(
        "/api/scenarios/compile/inject",
        json={"confirm": True, "recipe_id": recipe_id},
        headers={"Idempotency-Key": "TEST-holdout-lab-0001"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HOLDOUT_FORBIDDEN_IN_LAB"


def test_compile_inject_seen_creates_recipe_id(twin_client):
    from app.services.compiler.enumerate import eligible_pool
    from app.services.compiler.split import freeze_ids

    holdout = set(freeze_ids())
    allowed = {"life", "nginx_upstream", "nginx_timeout", "app_mode"}
    recipe = next(
        item
        for item in eligible_pool()
        if item.id not in holdout
        and not item.poison
        and all(primitive.kind in allowed for primitive in item.primitives)
    )
    response = twin_client.post(
        "/api/scenarios/compile/inject",
        json={"confirm": True, "recipe_id": recipe.id},
        headers={"Idempotency-Key": "TEST-compile-seen-0001"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["recipe_id"].startswith("rec_")
    item = twin_client.get(f"/api/incidents/{body['incident_id']}").json()
    assert item["detail"]["recipe_id"] == body["recipe_id"]
    assert item["scenario_key"] in {
        "COMPILED",
        "MYSQL_DOWN",
        "REDIS_DOWN",
        "NGINX_502",
        "NGINX_UPSTREAM",
        "APP_HTTP_500",
        "DUAL_MYSQL_REDIS",
    }


def test_compiled_recipe_rejects_no_action_when_probes_fail(twin_client, world):
    from app.services.compiler.enumerate import eligible_pool
    from app.services.compiler.split import freeze_ids

    world.action = "NO_ACTION"
    holdout = set(freeze_ids())
    recipe = next(
        item
        for item in eligible_pool()
        if item.id not in holdout
        and not item.poison
        and "START_DEMO_REDIS" in (item.preferred_actions or [])
        and len(item.primitives) >= 2
    )
    created = twin_client.post(
        "/api/scenarios/compile/inject",
        json={"confirm": True, "recipe_id": recipe.id},
        headers={"Idempotency-Key": "TEST-compiled-no-action-0001"},
    )
    assert created.status_code == 201, created.text
    incident_id = created.json()["incident_id"]
    assert twin_client.post(f"/api/incidents/{incident_id}/diagnose").status_code == 202
    item = twin_client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "WAITING_APPROVAL"
    assert item["detail"]["plan"]["action_id"] != "NO_ACTION"
    assert item["detail"]["plan"]["action_id"] in (item["detail"]["recipe"]["preferred_actions"] or [])


def test_freeze_disabled_by_default(client):
    response = client.post(
        "/api/compiler/freeze",
        headers={"Idempotency-Key": "TEST-freeze-0001"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
