"""反事实影子世界生命周期。见计划 §5.3。"""

from __future__ import annotations

import time
from contextlib import contextmanager

from filelock import FileLock, Timeout

from app.config import CHECK_IDS, DomainError, Settings
from app.integrations.docker import DockerController
from app.integrations.probes import Probes
from app.schemas.domain import Verdict, now
from app.services.action_catalog import perform
from app.services.compiler.apply import apply_recipe, undo_recipe
from app.services.compiler.schema import Recipe
from app.services.faults import clear_lab
from app.services.lab import lab_baseline


class TwinService:
    def __init__(
        self,
        settings: Settings,
        docker: DockerController,
        probes: Probes,
        store=None,
    ):
        self.settings = settings
        self.docker = docker
        self.probes = probes
        self.store = store
        self.lock_path = settings.data_dir / "twin.lock"

    @contextmanager
    def lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = FileLock(self.lock_path, timeout=0)
        try:
            handle.acquire()
        except Timeout as error:
            raise DomainError("TWIN_BUSY", "影子世界正在被另一场试修占用", 409) from error
        try:
            yield
        finally:
            handle.release()

    def locked(self) -> bool:
        return self.lock_path.exists() and FileLock(self.lock_path).is_locked

    def status(self) -> dict:
        try:
            baseline = lab_baseline(self.docker, self.probes)
        except DomainError as error:
            return {
                "status": "UNKNOWN",
                "healthy": False,
                "world": "twin",
                "compose_project": "aiops-twin",
                "services": {},
                "probes": [],
                "error": {"code": error.code, "message": error.message},
                "locked": self.locked(),
            }
        status = "PASS" if baseline["healthy"] else ("FAIL" if baseline["services"] else "UNKNOWN")
        if baseline.get("error"):
            status = "UNKNOWN"
        return {**baseline, "status": status, "locked": self.locked()}

    def available(self) -> bool:
        # UNKNOWN 才是「影子没起来」。注入后探针变红是 FAIL，试修仍应进行。
        return self.status().get("status") != "UNKNOWN"

    def ensure(self) -> dict:
        try:
            return self.docker.compose_up()
        except DomainError as error:
            raise DomainError(
                "TWIN_UNAVAILABLE",
                "无法拉起影子世界。本机需要再跑四个容器，内存不足时请关掉其他项目。",
                503,
            ) from error

    def baseline(self) -> bool:
        return bool(lab_baseline(self.docker, self.probes)["healthy"])

    def apply(self, recipe: Recipe) -> dict:
        return apply_recipe(self.docker, recipe)

    def reset_clean(self) -> list[dict]:
        return clear_lab(self.docker)

    def reset_to(self, recipe: Recipe) -> dict:
        """拆除一切 → 基线 → 再注入该配方。试修前必须走这条。"""
        self.reset_clean()
        if not self.baseline():
            # 再试一次启动
            for service in ("demo-mysql", "demo-redis", "demo-app", "demo-nginx"):
                try:
                    self.docker.change(service, "start")
                except DomainError:
                    pass
        self.apply(recipe)
        return {"recipe_id": recipe.id, "reset": True}

    def destroy(self, volumes: bool = False) -> dict:
        return self.docker.compose_down(volumes=volumes)

    def trial(self, recipe: Recipe, action_id: str, incident_id: str | None = None) -> dict:
        started = time.monotonic()
        try:
            with self.lock():
                if not self.available() and not self._try_ensure():
                    return {
                        "world": "twin",
                        "recipe_id": recipe.id,
                        "action_id": action_id,
                        "skipped": True,
                        "reason": "TWIN_UNAVAILABLE",
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                before = {probe.name: probe.verdict for probe in self.probes.baseline()}
                self.reset_to(recipe)
                before = {probe.name: probe.verdict.value for probe in self.probes.baseline()}
                perform(self.docker, action_id)
                failing = list((recipe.signature.failing_checks if recipe.signature else []) or [])
                if not failing:
                    failing = [name for name, verdict in before.items() if verdict == "FAIL"]
                after = {}
                for index in range(max(1, self.settings.verification_attempts)):
                    after_probes = self.probes.baseline()
                    after = {probe.name: probe.verdict.value for probe in after_probes}
                    if failing and all(after.get(name) == Verdict.PASS.value for name in failing):
                        break
                    # 连续 FAIL 不能当成「已经稳定失败」。MySQL 刚 start 时前两轮探针仍红，
                    # 提前停会把可恢复写成部分恢复，证书对不上。
                    if index + 1 < self.settings.verification_attempts:
                        time.sleep(self.settings.verification_interval)
                predicted_pass = [
                    name for name in CHECK_IDS if before.get(name) == "FAIL" and after.get(name) == "PASS"
                ]
                predicted_still_fail = [name for name in CHECK_IDS if after.get(name) == "FAIL"]
                would_resolve = bool(failing) and all(
                    after.get(name) == Verdict.PASS.value for name in failing
                )
                self.reset_to(recipe)
                payload = {
                    "world": "twin",
                    "recipe_id": recipe.id,
                    "action_id": action_id,
                    "before": before,
                    "after": after,
                    "predicted_pass": predicted_pass,
                    "predicted_still_fail": predicted_still_fail,
                    "would_resolve_signature": would_resolve,
                    "skipped": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
        except DomainError as error:
            if error.code in {"TWIN_BUSY"}:
                raise
            payload = {
                "world": "twin",
                "recipe_id": recipe.id,
                "action_id": action_id,
                "skipped": True,
                "reason": error.code,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        if self.store is not None:
            with self.store.session() as session, session.begin():
                from app.repositories.store import TwinTrial
                from app.schemas.domain import new_id

                trial_id = new_id("trial")
                session.add(
                    TwinTrial(
                        id=trial_id,
                        incident_id=incident_id,
                        evaluation_run_id=None,
                        recipe_id=recipe.id,
                        action_id=action_id,
                        before_json=payload.get("before") or {},
                        after_json=payload.get("after") or {},
                        skipped=int(bool(payload.get("skipped"))),
                        elapsed_ms=payload.get("elapsed_ms") or 0,
                        created_at=now(),
                    )
                )
                self.store.audit(session, incident_id, "twin_trial", payload)
                payload["id"] = trial_id
        return payload

    def _try_ensure(self) -> bool:
        try:
            self.ensure()
            return self.available()
        except DomainError:
            return False

    def compare_verdicts(self, primary_probes, twin_probes) -> dict:
        """两界探针 verdict 必须一致；不一致记 TWIN_DIVERGENCE。"""
        left = {
            probe.name: (probe.verdict.value if hasattr(probe.verdict, "value") else probe.verdict)
            for probe in primary_probes
        }
        right = {
            probe.name: (probe.verdict.value if hasattr(probe.verdict, "value") else probe.verdict)
            for probe in twin_probes
        }
        mismatch = [name for name in CHECK_IDS if left.get(name) != right.get(name)]
        if mismatch:
            raise DomainError(
                "TWIN_DIVERGENCE",
                "真环境与影子世界的探针判定不一致",
                409,
                {"mismatch": mismatch, "primary": left, "twin": right},
            )
        return {"match": True, "primary": left, "twin": right}

    def certificate(self, trial: dict, observed_probes: list) -> dict:
        predicted = trial.get("after") or {}
        observed = {
            probe.name: (probe.verdict.value if hasattr(probe.verdict, "value") else probe.verdict)
            for probe in observed_probes
        }
        match = {name: predicted.get(name) == observed.get(name) for name in CHECK_IDS}
        matched = sum(1 for name in CHECK_IDS if match.get(name))
        surprise = [
            name
            for name in CHECK_IDS
            if predicted.get(name) != observed.get(name) and predicted.get(name) and observed.get(name)
        ]
        return {
            "predicted": predicted,
            "observed": observed,
            "match": match,
            "match_rate": round(matched / len(CHECK_IDS), 4),
            "surprise": surprise,
            "trial_id": trial.get("id"),
        }
