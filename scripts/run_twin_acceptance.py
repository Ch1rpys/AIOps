"""真 Docker 验收：两界基线、配置不串写、canonical 保真度。没有 Docker 时退出 2。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _wait_healthy(docker, probes, label: str, rounds: int = 12) -> bool:
    from app.services.lab import lab_baseline

    last = None
    for _ in range(rounds):
        last = lab_baseline(docker, probes)
        if last.get("healthy"):
            return True
        time.sleep(2)
    print(f"WARN {label} not healthy services={last.get('services') if last else None}")
    return False


def _compare(twin, primary_probes, twin_probes, scene: str, attempts: int = 6) -> None:
    from app.config import DomainError

    last: DomainError | None = None
    for _ in range(attempts):
        try:
            twin.compare_verdicts(primary_probes.baseline(), twin_probes.baseline())
            return
        except DomainError as error:
            last = error
            time.sleep(1)
    details = last.details if last is not None else {}
    print(
        f"FAIL fidelity {scene}: {last.code if last else 'UNKNOWN'} "
        f"mismatch={details.get('mismatch')} primary={details.get('primary')} twin={details.get('twin')}"
    )
    raise last  # type: ignore[misc]


def main() -> int:
    from app.config import DomainError, Settings
    from app.integrations.docker import DockerController
    from app.integrations.probes import Probes
    from app.services.compiler.apply import apply_recipe, undo_recipe
    from app.services.compiler.canonical import canonical_recipes
    from app.services.compiler.enumerate import enumerate_recipes
    from app.services.compiler.split import freeze_ids
    from app.services.twin import TwinService
    from app.worlds import PRIMARY_NGINX_ACTIVE, world_from_settings
    import httpx

    settings = Settings()
    primary_world = world_from_settings(settings, "primary")
    twin_world = world_from_settings(settings, "twin")
    primary = DockerController(settings, primary_world)
    twin_docker = DockerController(settings, twin_world)
    try:
        primary.snapshot()
        twin_docker.snapshot()
    except DomainError as error:
        print(f"SKIP Docker 不可用: {error.code}")
        return 2

    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        primary_probes = Probes(settings, client, primary_world)
        twin_probes = Probes(settings, client, twin_world)
        twin = TwinService(settings, twin_docker, twin_probes)
        if not twin.baseline():
            print("FAIL twin baseline")
            return 1
        before = PRIMARY_NGINX_ACTIVE.read_bytes()
        twin_docker.apply_nginx_timeouts(1, 2, 2)
        after = PRIMARY_NGINX_ACTIVE.read_bytes()
        if before != after:
            print("FAIL primary nginx changed when twin wrote timeouts")
            return 1
        twin.reset_clean()
        if not _wait_healthy(twin_docker, twin_probes, "twin after isolation"):
            print("FAIL twin baseline after isolation write")
            return 1
        recipes = list(canonical_recipes().items())
        holdout = set(freeze_ids())
        sample = next(
            (
                item
                for item in enumerate_recipes()
                if item.id in holdout
                and len(item.primitives) == 2
                and all(
                    primitive.kind == "life" and primitive.verb == "stop"
                    for primitive in item.primitives
                )
            ),
            None,
        )
        if sample is not None:
            recipes.append((f"holdout:{sample.id}", sample))
        for scene, recipe in recipes:
            apply_recipe(primary, recipe)
            apply_recipe(twin_docker, recipe)
            try:
                _compare(twin, primary_probes, twin_probes, str(scene))
            except DomainError as error:
                undo_recipe(primary, recipe)
                undo_recipe(twin_docker, recipe)
                print(f"FAIL fidelity {scene}: {error.code}")
                return 1
            undo_recipe(primary, recipe)
            undo_recipe(twin_docker, recipe)
            _wait_healthy(primary, primary_probes, f"primary after {scene}")
            _wait_healthy(twin_docker, twin_probes, f"twin after {scene}")
            print(f"PASS fidelity {scene}")
        primary_net = {row["Name"] for row in _network_containers("aiops-demo_default")}
        twin_net = {row["Name"] for row in _network_containers("aiops-twin_default")}
        if primary_net & twin_net:
            print(f"FAIL networks share containers {primary_net & twin_net}")
            return 1
        print("PASS network isolation")
        from app.services.compiler.canonical import canonical_recipe
        from app.schemas.domain import Scenario

        twin.destroy()
        mysql = canonical_recipe(Scenario.MYSQL_DOWN)
        apply_recipe(primary, mysql)
        undo_recipe(primary, mysql)
        if not _wait_healthy(primary, primary_probes, "primary after twin down"):
            print("FAIL primary path after twin down")
            twin.ensure()
            return 1
        print("PASS primary path while twin down")
        twin.ensure()
        if not _wait_healthy(twin_docker, twin_probes, "twin after restore"):
            print("FAIL twin restore")
            return 1
    print("PASS twin acceptance")
    return 0


def _network_containers(name: str) -> list[dict]:
    import json
    import subprocess

    result = subprocess.run(
        ["docker", "--context", "default", "network", "inspect", name, "--format", "{{json .Containers}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    data = json.loads(result.stdout)
    return [{"Name": item.get("Name"), "IPv4": item.get("IPv4Address")} for item in (data or {}).values()]


if __name__ == "__main__":
    raise SystemExit(main())
