import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import DomainError
from app.scenarios import SCENARIOS
from app.schemas.domain import (
    ApprovalRequest,
    AttachmentRequest,
    Confirmation,
    EvaluationRequest,
    IncidentView,
    Scenario,
    Status,
)
from app.services.action_catalog import CATALOG
from app.services.metrics import adversarial_closed_loop, end_to_end_summary
from app.services.review import build_review, incident_impact_graph, render_review_markdown

router = APIRouter(prefix="/api")
logger = logging.getLogger("aiops.api")
Key = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
]


def platform(request: Request):
    return request.app.state.platform


def evaluation(request: Request):
    return request.app.state.evaluation


def require_admin(
    request: Request, token: Annotated[str | None, Header(alias="X-Admin-Token")] = None
):
    expected = request.app.state.platform.settings.admin_token.get_secret_value()
    if not expected:
        raise DomainError("ADMIN_NOT_CONFIGURED", "未设置 ADMIN_TOKEN，写操作不可用", 503)
    # 粘贴时常见的尾部换行或空格不应导致令牌被误判为错误。
    provided = (token or "").strip()
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise DomainError(
            "FORBIDDEN",
            "操作需要本机管理员令牌；请在右上角设置中粘贴 .env 里 ADMIN_TOKEN= 后面的值",
            403,
        )


admin = [Depends(require_admin)]


@router.get("/health")
def health(service=Depends(platform)):
    return service.health()


@router.get("/lab/status")
def lab_status(service=Depends(platform)):
    return service.environment()


@router.get("/action-catalog")
def action_catalog():
    return {
        "count": len(CATALOG),
        "note": "只收目录中的编号。另一条目录路线可以批准，复测通过才算修好。",
    }


@router.get("/scenarios")
def scenarios():
    return [
        {
            "key": key,
            "name": definition.name,
            "enabled": True,
            "backend_enabled": True,
            "target": definition.target,
            "checks": list(definition.checks),
            "description": definition.description,
            "risk": "仅限 aiops-demo 项目；需人工批准后恢复",
        }
        for key, definition in SCENARIOS.items()
    ]


@router.post("/scenarios/{scenario}/inject", dependencies=admin, status_code=201)
def inject(scenario: Scenario, body: Confirmation, key: Key, service=Depends(platform)):
    return service.inject(scenario, key, body.poison)


@router.post("/lab/reset", dependencies=admin)
def reset(body: Confirmation, key: Key, service=Depends(platform)):
    return service.reset(key)


@router.get("/incidents")
def incidents(
    status: Status | None = None,
    scenario: Scenario | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service=Depends(platform),
):
    return service.store.listing(status, limit, offset, scenario)


@router.get("/incidents/{incident_id}", response_model=IncidentView)
def incident(incident_id: str, service=Depends(platform)):
    return service.store.view(incident_id)


@router.post("/incidents/{incident_id}/diagnose", dependencies=admin, status_code=202)
def diagnose(incident_id: str, tasks: BackgroundTasks, service=Depends(platform)):
    result = service.start(incident_id)
    tasks.add_task(service.run, incident_id)
    return result


@router.post("/incidents/{incident_id}/approval", dependencies=admin, status_code=202)
def approval(
    incident_id: str,
    body: ApprovalRequest,
    key: Key,
    tasks: BackgroundTasks,
    service=Depends(platform),
):
    result, schedule = service.approve(incident_id, body, key)
    if schedule:
        tasks.add_task(service.run, incident_id, body.model_dump())
    return result


@router.get("/incidents/{incident_id}/steps")
def steps(incident_id: str, service=Depends(platform)):
    return [r for r in service.store.view(incident_id)["records"] if r["kind"] == "step"]


@router.get("/incidents/{incident_id}/evidence")
def evidence(incident_id: str, service=Depends(platform)):
    return [r for r in service.store.view(incident_id)["records"] if r["kind"] == "evidence"]


@router.get("/incidents/{incident_id}/verification")
def verification(incident_id: str, service=Depends(platform)):
    return service.store.view(incident_id)["detail"].get("verification")


@router.get("/incidents/{incident_id}/report")
def report(incident_id: str, service=Depends(platform)):
    return service.store.view(incident_id)


@router.post("/incidents/{incident_id}/attachments", dependencies=admin, status_code=201)
def add_attachment(
    incident_id: str,
    body: AttachmentRequest,
    service=Depends(platform),
):
    """上传一张真实错误页面或运维截图，并提取其中的观测。"""
    attachment = service.attachments.store(body.filename, body.content_base64)
    try:
        attachment = service.attachments.analyze(attachment, body.note)
    except DomainError:
        # 分析失败时刚存的文件没有任何工单引用，直接删掉，不留孤儿文件。
        service.attachments.discard(attachment)
        raise
    with service.store.session() as session, session.begin():
        item = service.store.require(session, incident_id)
        service.store.append_attachment(session, item, attachment)
        service.store.audit(session, incident_id, "attachment", attachment)
    return attachment


@router.get("/incidents/{incident_id}/attachments")
def list_attachments(incident_id: str, service=Depends(platform)):
    return service.store.view(incident_id)["detail"].get("attachments", [])


@router.get("/incidents/{incident_id}/impact")
def impact_graph(incident_id: str, service=Depends(platform)):
    """服务影响图：从工单已保存的证据生成，区分三类关系。"""
    return incident_impact_graph(service.store.view(incident_id))


@router.get("/incidents/{incident_id}/review")
def review(incident_id: str, service=Depends(platform)):
    """复盘摘要：只投影持久化记录，不现场重算。"""
    return build_review(service.store.view(incident_id))


@router.get("/incidents/{incident_id}/review.md", response_class=PlainTextResponse)
def review_markdown(incident_id: str, service=Depends(platform)):
    content = render_review_markdown(build_review(service.store.view(incident_id)))
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{incident_id}-review.md"'},
    )


@router.get("/incidents/{incident_id}/report.md", response_class=PlainTextResponse)
def report_markdown(incident_id: str, service=Depends(platform)):
    item = service.store.view(incident_id)
    content = [
        f"# 故障报告 · {item['id']}",
        "",
        f"模式：{item['mode']} | 场景：{item['scenario_key']} | 状态：{item['status']}",
        f"创建：{item['created_at']} | 结束：{item['finished_at'] or '未结束'}",
        "",
        "## 当前诊断、计划与复测",
        "",
        "```json",
        json.dumps(item["detail"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 审计记录",
    ]
    for record in item["records"]:
        content.extend(
            [
                "",
                f"### {record['created_at']} · {record['kind']}",
                "",
                "```json",
                json.dumps(record["payload"], ensure_ascii=False, indent=2),
                "```",
            ]
        )
    return PlainTextResponse(
        "\n".join(content),
        headers={"Content-Disposition": f'attachment; filename="{incident_id}.md"'},
    )


# ---- 盲诊评测（独立路由，不进工作台）----------------------------------------
# 真值只在 evaluation_runs.truth_scenario 一处，由评测服务读写。
# 默认返回体一律不含真值与判卷结果；只有显式的评测视图才带出来。


def evaluation_view(request: Request) -> bool:
    """评测视图开关：必须带管理员令牌，避免真值被顺手读走。"""
    if request.query_params.get("view") != "evaluation":
        return False
    require_admin(request, request.headers.get("X-Admin-Token"))
    return True


def run_evaluation_safely(service, scenario, arms, attachment_ids=None, cohort="standard") -> None:
    """后台执行评测：异常只记录，不让后台任务把响应周期搞崩。"""
    try:
        service.run_experiment(scenario, arms, attachment_ids or [], cohort)
    except DomainError as error:
        # 不打印场景：真值不进日志（§4.1）。
        logger.error("evaluation_failed code=%s", error.code)
    except Exception as error:  # pragma: no cover - 兜底，避免后台任务抛出
        logger.error("evaluation_failed type=%s", type(error).__name__)


@router.post("/evaluation/attachments", dependencies=admin, status_code=201)
def create_evaluation_attachment(
    body: AttachmentRequest,
    service=Depends(evaluation),
):
    """上传给评测用的截图（C 臂的视觉证据）。

    这里不创建工单：评测链路不产生 Incident，真值不进入任何上下文。
    """
    attachments = service.attachments
    attachment = attachments.store(body.filename, body.content_base64)
    try:
        attachment = attachments.analyze(attachment, body.note)
    except DomainError:
        attachments.discard(attachment)
        raise
    return {
        "id": attachment["id"],
        "filename": attachment["filename"],
        "mime": attachment["mime"],
        "observed_at": attachment.get("observed_at"),
        "observed_at_known": attachment.get("observed_at_known"),
        "anomalies": attachment.get("anomalies") or [],
        "uncertain": attachment.get("uncertain") or [],
    }


@router.post("/evaluation/runs", dependencies=admin, status_code=202)
def create_evaluation_run(
    body: EvaluationRequest,
    key: Key,
    tasks: BackgroundTasks,
    service=Depends(evaluation),
    platform_service=Depends(platform),
):
    if "C" in body.arms and not body.attachment_ids:
        # C 臂没有视觉证据就退化成 B 臂：那是假对照，明确拒绝而不是降级伪装。
        raise DomainError(
            "EVALUATION_ATTACHMENT_REQUIRED",
            "C 臂需要先上传截图（POST /api/evaluation/attachments）并带上 attachment_ids",
            409,
        )
    # 幂等指纹必须覆盖全部会影响实验内容的输入：截图换了就不是同一批实验。
    scope = "evaluation"
    arms = list(body.arms)
    if body.cohort == "adversarial" and "L" not in arms:
        arms.append("L")
    fingerprint = (
        f"{body.cohort}:{body.scenario.value}:{','.join(arms)}:"
        f"{','.join(sorted(body.attachment_ids))}"
    )
    with platform_service.store.session() as session:
        previous = platform_service.store.receipt(session, key, scope, fingerprint)
        if previous:
            return previous
        if platform_service.store.evaluation_busy():
            raise DomainError("EVALUATION_BUSY", "评测进行中，请等待当前评测结束", 409)
        if platform_service.store.lab_busy():
            raise DomainError("LAB_BUSY", "工作台实验进行中，请先完成或重置当前实验后再评测", 409)
    response = {
        "accepted": True,
        "scenario": body.scenario.value,
        "arms": arms,
        "cohort": body.cohort,
        "status": "QUEUED",
    }
    with platform_service.store.session() as session, session.begin():
        platform_service.store.save_receipt(session, key, scope, fingerprint, response)
    tasks.add_task(
        run_evaluation_safely, service, body.scenario, arms, body.attachment_ids, body.cohort
    )
    return response


@router.get("/evaluation/runs")
def list_evaluation_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    arm: str | None = None,
    service=Depends(evaluation),
):
    reveal = evaluation_view(request)
    return service.store.evaluation_runs(limit, offset, arm, reveal)


@router.get("/evaluation/summary")
def evaluation_summary(service=Depends(evaluation)):
    """纯诊断对照与端到端对照的指标分开呈现，不混为一谈。"""
    summary = service.store.evaluation_summary()
    end_to_end = end_to_end_summary(service.store)
    extra = adversarial_closed_loop(service.store)
    if extra["measured"]:
        # 对抗闭环臂的非法提议并进错误处置提议数，避免这条计数被显示成「没有问过」。
        proposals = end_to_end["proposals"] + extra["proposals"]
        illegal = end_to_end["illegal_proposals"] + extra["illegal_proposals"]
        blocked = end_to_end["blocked_proposals"] + extra["blocked_proposals"]
        end_to_end["proposals"] = proposals
        end_to_end["illegal_proposals"] = illegal
        end_to_end["blocked_proposals"] = blocked
        end_to_end["illegal_proposal_rate"] = round(illegal / proposals, 4) if proposals else None
        end_to_end["block_rate"] = round(blocked / illegal, 4) if illegal else None
        end_to_end["note"] = (
            "端到端指标统计闭环路径的处置提议，并计入对抗样本闭环臂被白名单拦住的提议。"
            "非法动作执行次数见对抗样本一节，不在这里记成 0。"
        )
    summary["end_to_end"] = end_to_end
    return summary


@router.get("/evaluation/runs/{run_id}")
def evaluation_run(run_id: str, request: Request, service=Depends(evaluation)):
    reveal = evaluation_view(request)
    return service.store.evaluation_run(run_id, reveal)
