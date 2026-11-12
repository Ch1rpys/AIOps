"""保真度套件：默认用夹具，不要求真 Docker。见计划 §5.8。"""

from app.schemas.domain import Probe, Verdict
from app.services.compiler.canonical import canonical_recipe
from app.services.twin import TwinService
from app.schemas.domain import Scenario


def _probes(failing: set[str]) -> list[Probe]:
    names = (
        "mysql_select_1",
        "app_db_check",
        "redis_roundtrip",
        "app_cache_check",
        "nginx_hello",
        "app_health",
    )
    return [
        Probe(
            name=name,
            verdict=Verdict.FAIL if name in failing else Verdict.PASS,
            observed={"mode": "TEST/MOCK"},
        )
        for name in names
    ]


def test_canonical_mysql_verdicts_match_across_worlds(settings, world):
    twin = TwinService(settings, world, world)
    recipe = canonical_recipe(Scenario.MYSQL_DOWN)
    failing = {"mysql_select_1", "app_db_check"}
    twin.compare_verdicts(_probes(failing), _probes(failing))
    assert recipe.canonical_scene == Scenario.MYSQL_DOWN
