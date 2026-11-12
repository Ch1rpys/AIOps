"""计划 P10：半边注入回滚、试修超时跳过、内存网格拒绝自由值。"""

import subprocess

import pytest
from app.config import DomainError, Settings
from app.integrations.docker import DockerController
from app.services.compiler.enumerate import eligible_pool
from app.services.compiler.schema import parse_recipe
from app.services.compiler.split import freeze_ids
from app.worlds import world_from_settings
from conftest import waiting


def _seen_recipe():
    holdout = set(freeze_ids())
    allowed = {"life", "nginx_upstream", "nginx_timeout", "app_mode"}
    return next(
        item
        for item in eligible_pool()
        if item.id not in holdout
        and not item.poison
        and all(primitive.kind in allowed for primitive in item.primitives)
    )


def test_half_inject_rolls_back_primary(twin_client, monkeypatch):
    from app.services import platform as platform_mod

    recipe = _seen_recipe()
    calls = {"n": 0}
    undone = {"n": 0}
    original_apply = platform_mod.apply_recipe
    original_undo = platform_mod.undo_recipe

    def fake_apply(docker, item):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("twin exploded")
        return original_apply(docker, item)

    def fake_undo(docker, item):
        undone["n"] += 1
        return original_undo(docker, item)

    monkeypatch.setattr(platform_mod, "apply_recipe", fake_apply)
    monkeypatch.setattr(platform_mod, "undo_recipe", fake_undo)
    response = twin_client.post(
        "/api/scenarios/compile/inject",
        json={"confirm": True, "recipe_id": recipe.id},
        headers={"Idempotency-Key": "TEST-half-inject-0001"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TWIN_UNAVAILABLE"
    assert undone["n"] >= 1


def test_trial_timeout_skips_prediction(twin_client, monkeypatch):
    def boom(*_a, **_k):
        raise DomainError("DOCKER_UNAVAILABLE", "影子动作超时", 503)

    monkeypatch.setattr("app.services.twin.perform", boom)
    incident_id, _plan_id = waiting(twin_client)
    trial = twin_client.get(f"/api/incidents/{incident_id}").json()["detail"]["twin_trial"]
    assert trial["skipped"] is True
    assert trial["reason"] == "DOCKER_UNAVAILABLE"


def test_mem_limit_out_of_grid_is_recipe_invalid():
    with pytest.raises(DomainError) as error:
        parse_recipe(
            {
                "title": "自由内存",
                "primitives": [{"kind": "mem_limit", "service": "demo-mysql", "memory": "999g"}],
            }
        )
    assert error.value.code == "RECIPE_INVALID"


def test_docker_timeout_is_unavailable(monkeypatch):
    settings = Settings()
    docker = DockerController(settings, world_from_settings(settings, "twin"))

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(DomainError) as error:
        docker.snapshot()
    assert error.value.code == "DOCKER_UNAVAILABLE"


def test_update_resources_rejects_free_memory():
    settings = Settings()
    docker = DockerController(settings, world_from_settings(settings, "primary"))
    docker._container = lambda _service: "demo-mysql"  # noqa: SLF001 - 只测网格，不碰引擎
    docker._run = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应调用 Docker"))
    with pytest.raises(DomainError) as error:
        docker.update_resources("demo-mysql", memory="1t")
    assert error.value.code == "BLOCKED"


def test_network_connect_reapplies_compose_alias():
    settings = Settings()
    docker = DockerController(settings, world_from_settings(settings, "twin"))
    captured: list[list[str]] = []
    docker._container = lambda _service: "cid-app"  # noqa: SLF001

    def capture(arguments, **_k):
        captured.append(list(arguments))
        return ""

    docker._run = capture
    docker.network("demo-app", "connect")
    assert any("--alias" in item and "demo-app" in item for item in captured)
    assert captured[-1][4:8] == ["connect", "--alias", "demo-app", docker.world.network]


def test_update_resources_sets_memory_swap_together():
    settings = Settings()
    docker = DockerController(settings, world_from_settings(settings, "primary"))
    captured: dict[str, list[str]] = {}
    docker._container = lambda _service: "cid-mysql"  # noqa: SLF001

    def capture(arguments, **_k):
        captured["arguments"] = arguments
        return ""

    docker._run = capture
    docker.update_resources("demo-mysql", memory="64m")
    assert captured["arguments"][4:8] == ["--memory", "64m", "--memory-swap", "64m"]
    docker.update_resources("demo-mysql", memory="0")
    assert captured["arguments"][4:8] == ["--memory", "0", "--memory-swap", "-1"]
