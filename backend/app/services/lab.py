"""实验环境的基线判定：工作台与评测共用同一份实现。

两处各写一份必然漂移，因此抽到这里：谁都要先确认
「四个 demo 服务与基线探针是否全部通过」才能动实验环境。
"""

from app.config import COMPOSE_PROJECT, SERVICES, DomainError
from app.integrations.docker import DockerController
from app.integrations.probes import Probes
from app.schemas.domain import Verdict


def lab_baseline(docker: DockerController, probes: Probes) -> dict:
    """容器状态快照 + 基线探针，并给出整体健康判定。"""
    states, error = {}, None
    try:
        states = docker.snapshot()
    except DomainError as failure:
        error = {"code": failure.code, "message": failure.message}
    checks = probes.baseline()
    healthy = (
        not error
        and all(
            states.get(s, {}).get("state") == "running"
            and states[s].get("health") == "healthy"
            and states[s].get("project") == COMPOSE_PROJECT
            for s in SERVICES
        )
        and all(p.verdict == Verdict.PASS for p in checks)
    )
    return {
        "healthy": healthy,
        "compose_project": COMPOSE_PROJECT,
        "services": states,
        "probes": [p.model_dump(mode="json") for p in checks],
        "error": error,
    }
