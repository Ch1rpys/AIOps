"""未见故障计分。旧正确率只用于 canonical 且 score_in_accuracy。"""

import re

from app.config import CHECK_IDS, SERVICES
from app.schemas.domain import Diagnosis
from app.services.compiler.schema import Recipe, RecipeSignature

SERVICE_ALIASES = {
    "demo-mysql": ("demo-mysql", "mysql", "数据库"),
    "demo-redis": ("demo-redis", "redis", "缓存"),
    "demo-app": ("demo-app", "应用", "upstream"),
    "demo-nginx": ("demo-nginx", "nginx", "代理"),
}


def claimed_checks(diagnosis: Diagnosis, weak: bool = True) -> tuple[list[str], str]:
    ids = list(diagnosis.failing_check_ids or [])
    if ids:
        return [item for item in ids if item in CHECK_IDS], "fields"
    if not weak:
        return [], "fields"
    mentioned: list[str] = []
    blob = " ".join([diagnosis.summary, *diagnosis.evidence_ids])
    for check in CHECK_IDS:
        if check in blob or check in diagnosis.evidence_ids:
            mentioned.append(check)
    return mentioned, "weak"


def claimed_services(diagnosis: Diagnosis, weak: bool = True) -> tuple[list[str], str]:
    ids = [item for item in (diagnosis.affected_services or []) if item in SERVICES]
    if ids:
        return ids, "fields"
    if not weak:
        return [], "fields"
    blob = diagnosis.summary
    mentioned: list[str] = []
    for service, aliases in SERVICE_ALIASES.items():
        if any(alias.lower() in blob.lower() for alias in aliases):
            mentioned.append(service)
    return mentioned, "weak"


def signature_scores(diagnosis: Diagnosis, signature: RecipeSignature) -> dict:
    failing = set(signature.failing_checks)
    claimed, parse = claimed_checks(diagnosis)
    claimed_set = set(claimed)
    recall = (len(claimed_set & failing) / len(failing)) if failing else None
    false_positive = [item for item in claimed if item not in failing]
    precision = (
        (len(claimed_set & failing) / len(claimed_set)) if claimed_set else (1.0 if not failing else 0.0)
    )
    services, service_parse = claimed_services(diagnosis)
    expected_services = set(signature.affected_services)
    union = set(services) | expected_services
    jaccard = (len(set(services) & expected_services) / len(union)) if union else None
    return {
        "signature_recall": round(recall, 4) if recall is not None else None,
        "signature_precision": round(precision, 4),
        "false_positive_checks": false_positive,
        "service_jaccard": round(jaccard, 4) if jaccard is not None else None,
        "parse": parse if parse == service_parse else f"{parse}+{service_parse}",
        "claimed_checks": claimed,
        "claimed_services": services,
    }


def score_unseen(diagnosis: Diagnosis, recipe: Recipe, *, drifted: bool = False) -> dict:
    signature = recipe.signature or RecipeSignature()
    if drifted:
        polite = diagnosis.root_cause in {"INSUFFICIENT_EVIDENCE", "OTHER"} and not diagnosis.evidence_sufficient
        return {
            "excluded": True,
            "reason": "SIGNATURE_DRIFT",
            "correct": 1 if polite else 0,
            "parse": "fields",
        }
    if recipe.negative:
        polite = diagnosis.root_cause in {"INSUFFICIENT_EVIDENCE", "OTHER"} and not diagnosis.evidence_sufficient
        return {
            "excluded": True,
            "reason": "negative",
            "correct": 1 if polite else 0,
            "parse": "fields",
        }
    scores = signature_scores(diagnosis, signature)
    enum_correct = None
    if recipe.canonical_scene or len(recipe.primitives) == 1:
        from app.scenarios import SCENARIOS
        from app.schemas.domain import Scenario

        scene = recipe.canonical_scene
        if scene is None and len(recipe.primitives) == 1:
            scene = None
        if scene is not None:
            truth = SCENARIOS[Scenario(scene)].root_cause
            enum_correct = int(diagnosis.root_cause == truth and diagnosis.evidence_sufficient)
    return {
        "excluded": False,
        "enum_correct": enum_correct,
        **scores,
    }


def strip_recipe_leakage(value):
    """从将要发给模型的结构里拿掉配方真值。"""
    blocked = {
        "recipe_id",
        "recipe",
        "primitives",
        "canonical_hash",
        "holdout",
        "signature",
        "signature_json",
        "preferred_actions",
        "canonical_scene",
        "compiler_version",
    }
    if isinstance(value, dict):
        return {
            key: strip_recipe_leakage(item)
            for key, item in value.items()
            if key not in blocked and not (isinstance(key, str) and key.startswith("rec_"))
        }
    if isinstance(value, list):
        return [strip_recipe_leakage(item) for item in value]
    if isinstance(value, str) and re.search(r"rec_[0-9a-f]{12}", value):
        return re.sub(r"rec_[0-9a-f]{12}", "rec_redacted", value)
    return value
