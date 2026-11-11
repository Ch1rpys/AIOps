"""泄漏防护测试：断言对象是最终序列化后的模型载荷。

见 docs/落地执行计划.md §4.3 与 §13.1。
只断言证据列表是不够的 —— provider 在最后一刻拼进去的字段照样会漏到模型眼前，
因此这里用假模型端点接住真实请求，断言请求体本身。
"""

import json

import httpx
import pytest
from app.agents.evidence import EvidenceAgent
from app.config import Settings
from app.integrations.models import HolmesDiagnosisProvider
from app.schemas.domain import Diagnosis, DomainReview

# 场景标识：泄漏即等于告诉模型「这次注入的是哪个场景」。
SCENARIO_MARKERS = [
    "MYSQL_DOWN",
    "REDIS_DOWN",
    "NGINX_502",
    "NGINX_UPSTREAM",
    "APP_HTTP_500",
    "DUAL_MYSQL_REDIS",
]
# 答案标签：泄漏即等于把答案写进提示词。
# 新标签在声明过的 Diagnosis 枚举里，出现在契约之外才算泄漏。
ANSWER_LABELS = [
    "MYSQL_SERVICE_STOPPED",
    "REDIS_SERVICE_STOPPED",
    "UPSTREAM_APP_STOPPED",
    "NGINX_UPSTREAM_MISCONFIGURED",
]
# 场景专属 Runbook 路径：内容里直接写着注入方式与根因。
SCENARIO_RUNBOOKS = [
    "knowledge/runbooks/mysql.md",
    "knowledge/runbooks/redis.md",
    "knowledge/runbooks/nginx.md",
    "knowledge/runbooks/nginx-upstream.md",
]
# 注册动作：泄漏即等于把处置答案写进提示词。
REGISTERED_ACTIONS = [
    "START_DEMO_MYSQL",
    "START_DEMO_REDIS",
    "START_DEMO_APP",
    "RESTORE_NGINX_UPSTREAM",
]
# 提示词里的指向性字段：泄漏即等于告诉模型该看哪几个探针。
POINTING_FIELDS = ["required_checks", "allowed_action", "expected_checks"]


class Recorder:
    """假模型端点：接住请求体，并回一份合规的诊断。"""

    def __init__(self, analysis: dict):
        self.analysis = analysis
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/api/info"):
            return httpx.Response(
                200, json={"version": "0.45.0", "toolsets": [{"name": "shell", "enabled": False}]}
            )
        return httpx.Response(200, json={"analysis": json.dumps(self.analysis, ensure_ascii=False)})

    @property
    def payload(self) -> dict:
        assert self.requests, "没有捕获到模型请求"
        return json.loads(self.requests[-1].content)


def blind_settings(tmp_path, mode: str) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        admin_token="TEST-admin-token",
        demo_mysql_password="private",
        holmes_json_mode=mode,
    )


def checkable_text(payload: dict) -> str:
    """可断言的载荷文本：剔除「声明过的答案空间」后剩下的全部内容。

    response_format 里声明的 Diagnosis schema 必然带有 root_cause 枚举，
    这是 §1.5 明确声明的「假设空间被收窄」限制，不是泄漏；
    除它之外，载荷的任何位置都不得出现场景标识或答案标签。
    """
    # 声明过的答案空间：诊断契约与复核契约的 root_cause 枚举。
    # json_object 模式下契约会被拼进提示词，因此字符串也要剔除。
    blobs = [
        json.dumps(Diagnosis.model_json_schema(), ensure_ascii=False),
        json.dumps(DomainReview.model_json_schema(), ensure_ascii=False),
    ]
    parts = []
    for key, value in payload.items():
        if key == "response_format":
            spec = dict(value)
            declared = spec.get("json_schema")
            if isinstance(declared, dict):
                spec["json_schema"] = {k: v for k, v in declared.items() if k != "schema"}
            parts.append(json.dumps(spec, ensure_ascii=False))
        elif isinstance(value, str):
            text = value
            for blob in blobs:
                text = text.replace(blob, "")
            parts.append(text)
        else:
            parts.append(json.dumps(value, ensure_ascii=False))
    return "\n".join(parts)


def test_current_closed_loop_path_still_leaks_scenario_markers(settings, world):
    """现状固化：闭环入口今天确实把场景标识与必检项塞进了模型载荷。

    这条测试是 §13.1 的第一步，证明「泄漏确实存在」是可执行的证据，
    而不是口头结论。目标态的约束由下面的盲诊用例承担。
    """
    recorder = Recorder(
        {
            "root_cause": "MYSQL_SERVICE_STOPPED",
            "summary": "TEST/MOCK",
            "evidence_ids": ["ev_1"],
            "confidence": 0.9,
            "evidence_sufficient": True,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    provider = HolmesDiagnosisProvider(settings, client)
    provider.diagnose(
        {
            "scenario_key": "MYSQL_DOWN",
            "evidence": [
                {
                    "id": "ev_1",
                    "source": "docker-compose",
                    "kind": "container_state",
                    "raw": {"demo-mysql": {"state": "exited"}},
                    "captured_at": "t0",
                }
            ],
        }
    )
    text = checkable_text(recorder.payload)
    assert "required_checks" in text
    assert "MYSQL_DOWN" in text


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_blind_payload_has_no_scenario_markers(tmp_path, world, mode):
    """目标态：盲诊发给模型的最终载荷里没有任何场景标识。"""
    settings = blind_settings(tmp_path, mode)
    evidence = EvidenceAgent(settings, world, world).collect_domain()
    ids = [item.id for item in evidence]
    recorder = Recorder(
        {
            "root_cause": "MYSQL_SERVICE_STOPPED",
            "summary": "TEST/MOCK",
            "evidence_ids": ids,
            "confidence": 0.9,
            "evidence_sufficient": True,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    provider = HolmesDiagnosisProvider(settings, client)
    provider.diagnose_domain({"evidence": [item.model_dump(mode="json") for item in evidence]})
    text = checkable_text(recorder.payload)
    for token in SCENARIO_MARKERS + SCENARIO_RUNBOOKS + REGISTERED_ACTIONS + POINTING_FIELDS:
        assert token not in text, f"盲诊载荷泄漏了 {token}"
    for label in ANSWER_LABELS:
        assert label not in text, f"盲诊载荷泄漏了答案标签 {label}"


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_review_payload_has_no_scenario_markers(tmp_path, world, mode):
    """B 臂的复核入口同样是盲诊路径：最终载荷里不能有场景标识。

    §4.3 要求对盲诊路径的最终序列化载荷断言，两个入口都要覆盖；
    两种 JSON 模式都要覆盖（json_object 会把契约拼进提示词）。
    """
    settings = blind_settings(tmp_path, mode)
    evidence = EvidenceAgent(settings, world, world).collect_domain()
    ids = [item.id for item in evidence]
    recorder = Recorder(
        {
            "verdict": "agree",
            "root_cause": "MYSQL_SERVICE_STOPPED",
            "reason": "TEST/MOCK",
            "evidence_ids": ids,
            "evidence_sufficient": True,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    provider = HolmesDiagnosisProvider(settings, client)
    provider.review_domain(
        {
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "previous_diagnosis": {
                "root_cause": "MYSQL_SERVICE_STOPPED",
                "summary": "TEST/MOCK",
                "evidence_ids": ids,
                "confidence": 0.9,
                "evidence_sufficient": True,
            },
        }
    )
    payload = recorder.payload
    text = checkable_text(payload)
    for token in SCENARIO_MARKERS + SCENARIO_RUNBOOKS + REGISTERED_ACTIONS + POINTING_FIELDS:
        assert token not in text, f"复核载荷泄漏了 {token}"
    # 复核者看得到第一次诊断自己的结论（那是模型自己的输出，不是真值），
    # 但除此之外，载荷里不能出现任何答案标签。
    ask = json.loads(payload["ask"])
    ask.pop("previous_diagnosis", None)
    without_previous = {**payload, "ask": json.dumps(ask, ensure_ascii=False)}
    for label in ANSWER_LABELS:
        assert label not in checkable_text(without_previous), f"复核载荷泄漏了答案标签 {label}"


def test_blind_payload_declares_the_narrowed_answer_space(tmp_path, world):
    """答案空间确实被根因枚举收窄：这一点必须在评测报告里显式声明。"""
    settings = blind_settings(tmp_path, "json_schema")
    evidence = EvidenceAgent(settings, world, world).collect_domain()
    recorder = Recorder(
        {
            "root_cause": "OTHER",
            "summary": "TEST/MOCK",
            "evidence_ids": [item.id for item in evidence],
            "confidence": 0.1,
            "evidence_sufficient": False,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    provider = HolmesDiagnosisProvider(settings, client)
    provider.diagnose_domain({"evidence": [item.model_dump(mode="json") for item in evidence]})
    declared = json.dumps(recorder.payload["response_format"], ensure_ascii=False)
    assert "MYSQL_SERVICE_STOPPED" in declared
    assert "INSUFFICIENT_EVIDENCE" in declared


def test_blind_payload_cites_every_required_evidence_id(tmp_path, world):
    """提示词要求与完整性校验同源：必须引用的 id 清单写进提示词。"""
    settings = blind_settings(tmp_path, "json_schema")
    evidence = EvidenceAgent(settings, world, world).collect_domain()
    recorder = Recorder(
        {
            "root_cause": "OTHER",
            "summary": "TEST/MOCK",
            "evidence_ids": [item.id for item in evidence],
            "confidence": 0.1,
            "evidence_sufficient": False,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    provider = HolmesDiagnosisProvider(settings, client)
    provider.diagnose_domain({"evidence": [item.model_dump(mode="json") for item in evidence]})
    prompt = recorder.payload["additional_system_prompt"]
    for item in evidence:
        if item.kind in {"container_state", "probe"}:
            assert item.id in prompt


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_attachment_payload_has_no_scenario_markers(tmp_path, world, mode):
    """C 臂的截图证据同样受泄漏约束：最终序列化载荷里不能有场景标识与答案标签。

    文件名是用户可控文本，截图 OCR 出来的文字是外部数据，
    两者都不能被当成「答案」或「指令」放进模型上下文（§4.3 / §5.2）。
    """
    settings = blind_settings(tmp_path, mode)
    evidence = EvidenceAgent(settings, world, world).collect_domain()
    ids = [item.id for item in evidence]
    recorder = Recorder(
        {
            "root_cause": "MYSQL_SERVICE_STOPPED",
            "summary": "TEST/MOCK",
            "evidence_ids": ids,
            "confidence": 0.9,
            "evidence_sufficient": True,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    provider = HolmesDiagnosisProvider(settings, client)
    attachment = {
        "id": "att_" + "a" * 64,
        "filename": "MYSQL_SERVICE_STOPPED.png",
        "sha256": "b" * 64,
        "storage": "attachments/" + "b" * 64 + ".png",
        "uploaded_at": "2026-11-02T10:00:00+00:00",
        "observed_at": "2026-11-02T09:00:00+00:00",
        "observed_at_known": True,
        "extracted_text": "503 Service Unavailable",
        "anomalies": [{"description": "数据库错误页", "region": [], "text": "503"}],
        "uncertain": [],
        "model": "TEST/MOCK",
    }
    payload_evidence = [
        *[item.model_dump(mode="json") for item in evidence],
        {
            "id": "evidence_att_" + "a" * 64,
            "source": "attachment:att_" + "a" * 64,
            "kind": "attachment",
            "raw": {
                "observed_at": attachment["observed_at"],
                "observed_at_known": True,
                "extracted_text": attachment["extracted_text"],
                "anomalies": attachment["anomalies"],
                "uncertain": [],
                "model": "TEST/MOCK",
            },
            "captured_at": "2026-11-02T10:00:00+00:00",
        },
    ]
    provider.diagnose_domain({"evidence": payload_evidence})
    text = checkable_text(recorder.payload)
    for token in SCENARIO_MARKERS + SCENARIO_RUNBOOKS + REGISTERED_ACTIONS + POINTING_FIELDS:
        assert token not in text, f"截图证据载荷泄漏了 {token}"
    # 用户可控的文件名与后台细节不得出现。
    assert "MYSQL_SERVICE_STOPPED.png" not in text
    assert "attachments/" not in text
    assert attachment["sha256"] not in text
