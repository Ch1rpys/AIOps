"""把配方注入影子，采全套探针，真值以校准为准。"""

import time

from app.config import CHECK_IDS, DomainError
from app.schemas.domain import Verdict, now
from app.services.compiler.apply import undo_recipe
from app.services.compiler.primitives import prior_affected_services, prior_failing_checks
from app.services.compiler.schema import Recipe, RecipeSignature
from app.services.compiler.titles import title_from_checks
from app.services.lab import lab_baseline


def signature_from_probes(probes, primitives) -> RecipeSignature:
    failing, passing, unknown = [], [], []
    for probe in probes:
        if probe.verdict == Verdict.FAIL:
            failing.append(probe.name)
        elif probe.verdict == Verdict.PASS:
            passing.append(probe.name)
        else:
            unknown.append(probe.name)
    affected = prior_affected_services(primitives)
    return RecipeSignature(
        failing_checks=failing,
        passing_checks=passing,
        unknown_checks=unknown,
        affected_services=affected,
        calibrated_at=now(),
        source="calibrated",
    )


def calibrate_recipe(twin, recipe: Recipe) -> Recipe:
    """twin 是 TwinService。失败时抛 CALIBRATION_DIRTY / PRIMITIVE_UNSUPPORTED。"""
    with twin.lock():
        twin.ensure()
        if not twin.baseline():
            twin.reset_clean()
        if not twin.baseline():
            raise DomainError("TWIN_UNAVAILABLE", "影子基线不健康，无法校准", 503)
        try:
            twin.apply(recipe)
        except DomainError as error:
            try:
                undo_recipe(twin.docker, recipe)
            except DomainError:
                twin.reset_clean()
            if error.code in {"PRIMITIVE_UNSUPPORTED", "DOCKER_COMMAND_FAILED"}:
                recipe.unsupported = True
                raise DomainError("PRIMITIVE_UNSUPPORTED", error.message, 409) from error
            raise
        probes = twin.probes.baseline()
        for _ in range(max(0, twin.settings.fault_confirmation_attempts - 1)):
            if any(probe.verdict == Verdict.FAIL for probe in probes):
                break
            time.sleep(twin.settings.fault_confirmation_interval)
            probes = twin.probes.baseline()
        signature = signature_from_probes(probes, recipe.primitives)
        try:
            signature.twin_image_digests = twin.docker.image_digests()
        except (DomainError, AttributeError):
            signature.twin_image_digests = {}
        try:
            undo_recipe(twin.docker, recipe)
        except DomainError:
            twin.reset_clean()
        if not twin.baseline():
            twin.reset_clean()
        if not twin.baseline():
            recipe.dirty = True
            raise DomainError("CALIBRATION_DIRTY", "校准后影子回不了基线", 409)
        if not signature.failing_checks and not recipe.negative:
            raise DomainError("RECIPE_INVALID", "非负例校准后没有任何失败探针", 409)
        recipe.signature = signature
        recipe.calibrated_at = signature.calibrated_at
        recipe.title = title_from_checks(signature.failing_checks) if signature.failing_checks else recipe.title
        recipe.dirty = False
        recipe.unsupported = False
        return recipe


def prior_signature(recipe: Recipe) -> RecipeSignature:
    failing = sorted(prior_failing_checks(recipe.primitives))
    passing = [name for name in CHECK_IDS if name not in failing]
    return RecipeSignature(
        failing_checks=failing,
        passing_checks=passing,
        unknown_checks=[],
        affected_services=prior_affected_services(recipe.primitives),
        source="prior",
    )


def probes_match_signature(probes, signature: RecipeSignature) -> bool:
    by_name = {probe.name: probe.verdict for probe in probes}
    for name in signature.failing_checks:
        if by_name.get(name) != Verdict.FAIL:
            return False
    return True


def drift_against_baseline(probes, signature: RecipeSignature) -> bool:
    return not probes_match_signature(probes, signature)
