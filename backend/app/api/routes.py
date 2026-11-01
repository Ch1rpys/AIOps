import hmac
import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import DomainError
from app.scenarios import SCENARIOS
from app.schemas.domain import ApprovalRequest, Confirmation, IncidentView, Scenario, Status

router = APIRouter(prefix="/api")
Key = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
]


def platform(request: Request):
    return request.app.state.platform


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
    return service.inject(scenario, key)


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
