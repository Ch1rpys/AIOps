"""故障编译器：DSL、哈希、约束、枚举幂等、holdout。见计划 §3 / §12.1。"""

import json

import pytest
from app.config import DomainError
from app.schemas.domain import Scenario
from app.services.compiler.canonical import canonical_hashes, canonical_recipe
from app.services.compiler.enumerate import enumerate_recipes, legal
from app.services.compiler.hash import canonical_hash, identify
from app.services.compiler.schema import Primitive, parse_recipe
from app.services.compiler.split import check_holdout, freeze_ids, load_holdout
from app.services.compiler.titles import title_from_prior


def test_recipe_id_is_derived_from_canonical_hash_not_title():
    left = [Primitive(kind="life", service="demo-mysql", verb="stop")]
    right = [Primitive(kind="life", service="demo-mysql", verb="stop")]
    assert identify(left) == identify(right)
    recipe_id, digest = identify(left)
    assert recipe_id == f"rec_{digest[:12]}"
    assert canonical_hash(left) == digest


def test_title_does_not_change_hash():
    primitives = [{"kind": "life", "service": "demo-redis", "verb": "stop"}]
    first = parse_recipe({"title": "甲", "primitives": primitives})
    second = parse_recipe({"title": "乙", "primitives": primitives})
    assert first.id == second.id
    assert first.canonical_hash == second.canonical_hash


def test_hand_written_id_must_match_hash():
    with pytest.raises(DomainError) as error:
        parse_recipe(
            {
                "id": "rec_deadbeefdead",
                "title": "错",
                "primitives": [{"kind": "life", "service": "demo-mysql", "verb": "stop"}],
            }
        )
    assert error.value.code == "RECIPE_INVALID"


def test_canonical_six_scenes_are_stable():
    hashes = canonical_hashes()
    assert set(hashes) == {item.value for item in Scenario}
    mysql = canonical_recipe(Scenario.MYSQL_DOWN)
    assert mysql.canonical_scene == Scenario.MYSQL_DOWN
    assert mysql.id.startswith("rec_")
    again = canonical_hashes()
    assert hashes == again


def test_table_b_and_recreate_have_static_priors():
    from app.services.compiler.primitives import prior_failing_checks

    assert prior_failing_checks([Primitive(kind="life", service="demo-mysql", verb="recreate")])
    assert prior_failing_checks([Primitive(kind="cpu_limit", service="demo-app", cpus="0.1")])
    assert prior_failing_checks([Primitive(kind="mem_limit", service="demo-redis", memory="32m")])
    assert prior_failing_checks([Primitive(kind="netem_delay", service="demo-nginx", delay_ms=50)])
    assert prior_failing_checks([Primitive(kind="netem_loss", service="demo-mysql", loss_pct=10)])
    assert prior_failing_checks([Primitive(kind="redis_op", op="flushdb")])


def test_constraint_rejects_four_stops_and_stop_plus_pause():
    stops = [
        Primitive(kind="life", service=service, verb="stop")
        for service in ("demo-mysql", "demo-redis", "demo-app", "demo-nginx")
    ]
    assert legal(stops) is False
    assert legal(
        [
            Primitive(kind="life", service="demo-mysql", verb="stop"),
            Primitive(kind="life", service="demo-mysql", verb="pause"),
        ]
    ) is False


def test_titles_describe_symptoms_not_root_cause_enum():
    primitives = [Primitive(kind="life", service="demo-mysql", verb="stop")]
    title = title_from_prior(primitives)
    assert "MYSQL_SERVICE_STOPPED" not in title
    assert "MYSQL_DOWN" not in title


def test_enumerate_is_idempotent():
    first = [recipe.canonical_hash for recipe in enumerate_recipes()]
    second = [recipe.canonical_hash for recipe in enumerate_recipes()]
    assert first == second
    assert len(set(first)) == len(first)


def test_holdout_excludes_canonical_and_has_enough_dual():
    result = check_holdout()
    assert result["ok"] is True
    assert result["dual"] >= 20
    document = load_holdout()
    canonical_ids = {canonical_recipe(scene).id for scene in Scenario}
    assert set(document["ids"]).isdisjoint(canonical_ids)
    assert document["ids"] == freeze_ids()


def test_compiler_recipes_list_from_generated_jsonl(client):
    response = client.get("/api/compiler/recipes?view=seen")
    assert response.status_code == 200
    body = response.json()
    assert body["holdout_size"] >= 20
    assert body["enumerated_size"] > 1000
    assert body["recipes"]
    assert "primitives" not in body["recipes"][0]


def test_holdout_list_requires_admin_token(client):
    response = client.get("/api/compiler/recipes?view=holdout", headers={"X-Admin-Token": ""})
    assert response.status_code == 403


def test_generated_jsonl_rebuilds_same_ids():
    from app.config import ROOT

    path = ROOT / "knowledge/compiler/generated.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    live = {recipe.id for recipe in enumerate_recipes()}
    assert {row["id"] for row in lines} == live
