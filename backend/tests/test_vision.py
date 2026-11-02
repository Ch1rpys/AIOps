"""M2：多模态视觉证据（docs/落地执行计划.md §5）。

三种验收情形缺一不可：
1. 清晰截图能正确提取观测；
2. 模糊或缺时间的截图明确表达不确定，不硬编一个时间；
3. 历史截图显示异常、当前探针健康时，提示时间或证据冲突并补查，不触发恢复。
"""

import base64

import pytest
from app.schemas.domain import VisionAnomaly, VisionObservation
from conftest import injected

PNG = b"\x89PNG\r\n\x1a\n" + b"TEST-MOCK-image-bytes"
JPEG = b"\xff\xd8\xff\xe0" + b"TEST-MOCK-image-bytes"


def upload(client, incident_id, content=PNG, filename="shot.png", note=""):
    response = client.post(
        f"/api/incidents/{incident_id}/attachments",
        json={
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "note": note,
        },
    )
    return response


def test_clear_screenshot_yields_observations(client, world):
    world.vision_observation = VisionObservation(
        observed_at="2026-11-02T10:00:00",
        extracted_text="502 Bad Gateway · upstream demo-app refused connections",
        anomalies=[VisionAnomaly(description="代理错误页", region=[0, 0, 800, 600], text="502")],
    )
    incident_id = injected(client)
    response = upload(client, incident_id)
    assert response.status_code == 201, response.text
    attachment = response.json()
    assert attachment["observed_at"] == "2026-11-02T10:00:00"
    assert attachment["observed_at_known"] is True
    assert "502" in attachment["extracted_text"]
    assert attachment["anomalies"][0]["description"] == "代理错误页"
    assert attachment["sha256"]
    assert attachment["uploaded_at"]
    assert attachment["model"]


def test_screenshot_without_time_is_explicitly_unknown(client, world):
    """缺时间的截图必须说「时间未知」，不能拿上传时间顶替。"""
    world.vision_observation = VisionObservation(
        observed_at=None,
        extracted_text="模糊的错误提示",
        anomalies=[VisionAnomaly(description="无法辨清的提示", region=[], text="")],
        uncertain=["画面模糊，无法确认错误码"],
    )
    incident_id = injected(client)
    attachment = upload(client, incident_id).json()
    assert attachment["observed_at"] is None
    assert attachment["observed_at_known"] is False
    assert "截图内没有可识别的观测时间，时间未知" in attachment["uncertain"]
    assert attachment["uploaded_at"]  # 上传时间仍然记录，但不能冒充观测时间


def test_historical_screenshot_conflict_blocks_recovery(client, world):
    """历史截图显示异常、当前探针健康：提示冲突并补查，不触发恢复。"""
    world.vision_observation = VisionObservation(
        observed_at="2026-11-01T03:00:00",
        extracted_text="503 Service Unavailable",
        anomalies=[VisionAnomaly(description="数据库错误页", region=[], text="503")],
    )
    incident_id = injected(client)
    upload(client, incident_id)
    # 注入后探针恢复正常：截图是历史异常，与当前实测冲突。
    world.stopped = False
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "UNKNOWN"
    assert item["detail"]["error"]["code"] == "EVIDENCE_CONFLICT"
    assert "时间或证据冲突" in item["detail"]["error"]["message"]
    # 没有进入审批页，也没有执行任何动作。
    assert "plan_id" not in item["detail"]
    assert world.operations == [("demo-mysql", "stop")]


def test_attachment_never_decides_recovery_by_itself(client, world):
    """图片本身不决定恢复：判定仍以探针实测为准。"""
    world.vision_observation = VisionObservation(
        observed_at="2026-11-02T10:00:00",
        extracted_text="一切正常",
        anomalies=[],
    )
    incident_id = injected(client)
    upload(client, incident_id)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "WAITING_APPROVAL"
    evidence = [
        record["payload"]
        for record in item["records"]
        if record["kind"] == "evidence" and record["payload"]["kind"] == "attachment"
    ]
    assert evidence
    assert evidence[0]["raw"]["note"].startswith("截图只作为线索")


def test_only_limited_image_formats_are_accepted(client, world):
    incident_id = injected(client)
    assert upload(client, incident_id, content=b"GIF89a-not-allowed").status_code == 415
    assert upload(client, incident_id, content=b"not-an-image").status_code == 415
    assert upload(client, incident_id, content=b"").status_code == 422
    assert upload(client, incident_id, content=JPEG).status_code == 201


def test_oversized_attachment_is_rejected(client, world):
    client.app.state.platform.settings.attachment_max_bytes = 1024
    incident_id = injected(client)
    assert upload(client, incident_id, content=PNG + b"x" * 4096).status_code == 413


def test_health_reports_vision_as_unknown_when_not_configured(client):
    result = client.get("/api/health").json()
    assert result["vision"]["status"] == "UNKNOWN"
    assert result["vision"]["configured"] is True  # TEST/MOCK 提供假视觉模型
    assert result["vision"]["reason"]


def test_evidence_attachment_field_is_optional():
    from app.schemas.domain import Evidence

    plain = Evidence(source="docker-compose", kind="container_state", raw={})
    assert plain.attachment is None
    with pytest.raises(ValueError):
        Evidence(source="x", kind="probe", raw={}, attachment="not-a-dict")
