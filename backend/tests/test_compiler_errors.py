"""计划 §8 点名的错误码。"""

from pathlib import Path

import pytest
from app.config import DomainError
from app.schemas.domain import Probe, Verdict
from app.services.compiler.calibrate import calibrate_recipe
from app.services.compiler.canonical import canonical_recipe
from app.services.compiler.schema import parse_recipe
from app.services.compiler.split import check_holdout
from app.services.twin import TwinService
from app.schemas.domain import Scenario


def test_recipe_invalid_dsl():
    with pytest.raises(DomainError) as error:
        parse_recipe({"title": "x", "primitives": [{"kind": "life", "verb": "explode"}]})
    assert error.value.code == "RECIPE_INVALID"


def test_compiler_version_mismatch(tmp_path: Path):
    path = tmp_path / "holdout.json"
    path.write_text(
        '{"frozen_at":"t","compiler_version":99,"algorithm":"x","canonical_excluded":[],"ids":[]}',
        encoding="utf-8",
    )
    with pytest.raises(DomainError) as error:
        check_holdout(path)
    assert error.value.code == "COMPILER_VERSION_MISMATCH"


def test_twin_unavailable_without_service(client):
    response = client.post(
        "/api/twin/ensure",
        headers={"Idempotency-Key": "TEST-twin-ensure-0001"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TWIN_UNAVAILABLE"


def test_calibration_dirty(settings, world):
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)

    class DirtyTwin(TwinService):
        def baseline(self) -> bool:
            if getattr(self, "_after", False):
                return False
            return True

        def reset_clean(self):
            self._after = True
            return []

        def apply(self, recipe):
            self._after = True
            return {"exit_code": 0}

        def ensure(self):
            return {"exit_code": 0}

    world.baseline = lambda: [
        Probe(name=name, verdict=Verdict.FAIL if name == "mysql_select_1" else Verdict.PASS, observed={})
        for name in (
            "mysql_select_1",
            "app_db_check",
            "redis_roundtrip",
            "app_cache_check",
            "nginx_hello",
            "app_health",
        )
    ]
    twin = DirtyTwin(settings, world, world)
    with pytest.raises(DomainError) as error:
        calibrate_recipe(twin, recipe)
    assert error.value.code == "CALIBRATION_DIRTY"


def test_primitive_unsupported():
    from types import SimpleNamespace

    from app.services.compiler.primitives import apply_primitive
    from app.services.compiler.schema import Primitive

    docker = SimpleNamespace(
        apply_netem=lambda *_a, **_k: (_ for _ in ()).throw(
            DomainError("PRIMITIVE_UNSUPPORTED", "需要 NET_ADMIN", 409)
        )
    )
    with pytest.raises(DomainError) as error:
        apply_primitive(docker, Primitive(kind="netem_delay", service="demo-mysql", delay_ms=50))
    assert error.value.code == "PRIMITIVE_UNSUPPORTED"
