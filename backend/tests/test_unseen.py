"""未见故障计分、漂移剔除、评测只抽 holdout。见计划 §4。"""

import pytest
from app.config import DomainError
from app.schemas.domain import Diagnosis
from app.services.compiler.canonical import canonical_recipe
from app.services.compiler.schema import RecipeSignature
from app.services.compiler.score import score_unseen
from app.services.compiler.split import freeze_ids
from app.schemas.domain import Scenario


def _diagnosis(**extra) -> Diagnosis:
    payload = {
        "root_cause": "OTHER",
        "summary": "TEST/MOCK 未见",
        "evidence_ids": ["ev_1"],
        "confidence": 0.5,
        "evidence_sufficient": True,
        **extra,
    }
    return Diagnosis.model_validate(payload)


def test_signature_recall_and_precision():
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)
    recipe.signature = RecipeSignature(
        failing_checks=["mysql_select_1", "app_db_check"],
        passing_checks=["nginx_hello"],
        affected_services=["demo-mysql"],
        source="calibrated",
    )
    diagnosis = _diagnosis(
        failing_check_ids=["mysql_select_1", "app_db_check"],
        affected_services=["demo-mysql"],
    )
    scores = score_unseen(diagnosis, recipe)
    assert scores["excluded"] is False
    assert scores["signature_recall"] == 1.0
    assert scores["signature_precision"] == 1.0


def test_drift_is_excluded_not_model_error():
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)
    recipe.signature = RecipeSignature(failing_checks=["mysql_select_1"], source="calibrated")
    scores = score_unseen(
        _diagnosis(root_cause="INSUFFICIENT_EVIDENCE", evidence_sufficient=False),
        recipe,
        drifted=True,
    )
    assert scores["excluded"] is True
    assert scores["reason"] == "SIGNATURE_DRIFT"
    assert scores["correct"] == 1


def test_empty_unseen_fields_omitted_from_old_dump():
    diagnosis = _diagnosis()
    dumped = diagnosis.model_dump(mode="json")
    assert "affected_services" not in dumped
    assert "failing_check_ids" not in dumped


def test_unseen_rejects_seen_recipe(client):
    from app.services.compiler.enumerate import eligible_pool
    from app.services.compiler.split import freeze_ids as frozen

    holdout = set(frozen())
    seen = next(recipe for recipe in eligible_pool() if recipe.id not in holdout)
    response = client.post(
        "/api/evaluation/runs",
        json={"cohort": "unseen", "recipe_id": seen.id, "arms": ["A"], "confirm": True},
        headers={"Idempotency-Key": "TEST-unseen-seen-0001"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SEEN_FORBIDDEN_IN_UNSEEN"


def test_unseen_requires_calibrated_signature(client):
    recipe_id = freeze_ids()[0]
    with pytest.raises(DomainError) as error:
        client.app.state.evaluation.run_experiment(
            None, ["A"], cohort="unseen", recipe_id=recipe_id
        )
    assert error.value.code == "RECIPE_NOT_CALIBRATED"


def test_r_arm_rejected_in_unseen(client):
    with pytest.raises(DomainError) as error:
        client.app.state.evaluation.run_experiment(None, ["R"], cohort="unseen")
    assert error.value.code == "EVALUATION_ARM_UNSUPPORTED"


def test_unseen_blind_does_not_call_planner_or_executor(client, world, monkeypatch):
    from app.services.compiler.enumerate import enumerate_recipes
    from app.services.compiler.schema import RecipeSignature

    recipe_id = freeze_ids()[0]
    recipe = next(item for item in enumerate_recipes() if item.id == recipe_id)
    recipe.signature = RecipeSignature(
        failing_checks=["mysql_select_1", "app_db_check"],
        passing_checks=["redis_roundtrip", "app_cache_check", "nginx_hello", "app_health"],
        affected_services=["demo-mysql"],
        source="calibrated",
        calibrated_at="t0",
    )
    recipe.calibrated_at = "t0"
    recipe.holdout = True
    client.app.state.evaluation.store.upsert_recipe(recipe)

    def fake_apply(*_a, **_k):
        world.change("demo-mysql", "stop")
        return {"ok": True}

    def fake_undo(*_a, **_k):
        world.change("demo-mysql", "start")
        return {"ok": True}

    monkeypatch.setattr("app.services.compiler.apply.apply_recipe", fake_apply)
    monkeypatch.setattr("app.services.compiler.apply.undo_recipe", fake_undo)
    world.planner_error = True
    result = client.app.state.evaluation.run_experiment(
        None, ["A"], cohort="unseen", recipe_id=recipe_id
    )
    assert result["cohort"] == "unseen"
    assert world.blind_contexts
    assert "recipe_id" not in world.blind_contexts[-1]
    assert "primitives" not in world.blind_contexts[-1]
    assert not any(op[1] == "plan" for op in world.operations if len(op) > 1)


def test_unseen_summary_hides_recipe_ids(client):
    summary = client.get("/api/evaluation/unseen/summary").json()
    text = str(summary)
    assert "primitives" not in text
    assert "canonical_hash" not in text
    assert "弱解析" in summary["note"]
