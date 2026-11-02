import json
import subprocess

import httpx
import pymysql
import pytest
from app.config import DomainError, Settings
from app.integrations.docker import DockerController
from app.integrations.models import HolmesDiagnosisProvider, LLMPlanner
from app.integrations.probes import Probes, judge
from app.schemas.domain import Plan, Probe, Verdict
from pydantic import ValidationError


def holmes_transport(handler):
    def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "0.45.0", "toolsets": []})
        return handler(request)

    return httpx.MockTransport(respond)


def test_holmes_decodes_analysis_json_string(settings):
    # M3 扩展：candidates 与 requested_checks 是可选字段，未返回时取空列表。
    expected = {
        "root_cause": "MYSQL_SERVICE_STOPPED",
        "summary": "TEST/MOCK",
        "evidence_ids": ["ev_test"],
        "confidence": 0.8,
        "evidence_sufficient": True,
        "candidates": [],
        "requested_checks": [],
    }

    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert payload["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, json={"analysis": json.dumps(expected)})

    with httpx.Client(transport=holmes_transport(handler)) as client:
        assert HolmesDiagnosisProvider(settings, client).diagnose({}).model_dump() == expected


def test_holmes_nginx_context_requires_only_proxy_probe(settings):
    def handler(request):
        payload = json.loads(request.content)
        context = json.loads(payload["ask"])
        assert context["required_checks"] == ["nginx_hello"]
        assert context["scenario_key"] == "NGINX_502"
        return httpx.Response(
            200,
            json={
                "analysis": json.dumps(
                    {
                        "root_cause": "UPSTREAM_APP_STOPPED",
                        "summary": "TEST/MOCK",
                        "evidence_ids": ["TEST-proxy"],
                        "confidence": 0.9,
                        "evidence_sufficient": True,
                    }
                )
            },
        )

    with httpx.Client(transport=holmes_transport(handler)) as client:
        result = HolmesDiagnosisProvider(settings, client).diagnose({"scenario_key": "NGINX_502"})
        assert result.evidence_sufficient


@pytest.mark.parametrize(
    "body", [{}, {"analysis": "bad json"}, {"analysis": {"root_cause": "wrong"}}]
)
def test_holmes_invalid_schema(settings, body):
    with httpx.Client(
        transport=holmes_transport(lambda r: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(DomainError, match="analysis") as error:
            HolmesDiagnosisProvider(settings, client).diagnose({})
        assert error.value.code == "AI_INVALID_RESPONSE"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_holmes_http_failure_no_fallback(settings, status):
    with httpx.Client(transport=holmes_transport(lambda r: httpx.Response(status))) as client:
        with pytest.raises(DomainError) as error:
            HolmesDiagnosisProvider(settings, client).diagnose({})
        assert error.value.code == "AI_UNAVAILABLE"


def test_planner_rejects_arbitrary_action(settings):
    settings.planner_url = "http://test/v1"
    settings.planner_model = "TEST/MOCK"
    from pydantic import SecretStr

    settings.planner_api_key = SecretStr("TEST/MOCK")
    raw = {
        "action_id": "DELETE_ALL",
        "target": "demo-mysql",
        "reason": "TEST/MOCK",
        "risk": "LOW",
        "expected_checks": ["mysql_select_1", "app_db_check"],
    }
    body = {"choices": [{"message": {"content": json.dumps(raw)}}]}
    with httpx.Client(
        transport=holmes_transport(lambda r: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(DomainError) as error:
            LLMPlanner(settings, client).plan({})
        assert error.value.code == "BLOCKED"


@pytest.mark.parametrize(
    "values,expected",
    [
        ([Verdict.PASS, Verdict.PASS], Verdict.PASS),
        ([Verdict.PASS, Verdict.FAIL], Verdict.FAIL),
        ([Verdict.FAIL, Verdict.UNKNOWN], Verdict.UNKNOWN),
        ([Verdict.PASS], Verdict.UNKNOWN),
    ],
)
def test_independent_verdict(values, expected):
    probes = [
        Probe(name=name, verdict=value, observed={})
        for name, value in zip(("mysql_select_1", "app_db_check"), values, strict=False)
    ]
    assert judge(probes) == expected


def test_probe_missing_credentials_and_unreachable_http_are_unknown():
    def offline(request):
        raise httpx.ConnectError("TEST/MOCK offline", request=request)

    with httpx.Client(transport=holmes_transport(offline)) as client:
        probes = Probes(Settings(_env_file=None), client).mysql()
        assert all(p.verdict == Verdict.UNKNOWN for p in probes)


@pytest.mark.parametrize(
    "body,status,expected",
    [
        ({"status": "ok", "value": 1}, 200, Verdict.PASS),
        ({"status": "error"}, 503, Verdict.FAIL),
        ({"status": "ok", "value": 2}, 200, Verdict.FAIL),
    ],
)
def test_business_probe_checks_body(settings, body, status, expected):
    with httpx.Client(
        transport=holmes_transport(lambda r: httpx.Response(status, json=body))
    ) as client:
        result = Probes(settings, client).http(
            "app_db_check", "http://test/db-check", {"status": "ok", "value": 1}
        )
        assert result.verdict == expected


def test_docker_never_accepts_freeform_commands(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(DomainError):
        DockerController(settings).change("production-db", "rm")
    assert calls == []


def test_plan_forbids_shell_extra_field():
    with pytest.raises(ValidationError):
        Plan(
            action_id="START_DEMO_MYSQL",
            target="demo-mysql",
            risk="LOW",
            reason="test",
            expected_checks=["mysql_select_1", "app_db_check"],
            shell="anything",
        )


def test_holmes_json_object_mode_still_validates_schema(settings):
    settings.holmes_json_mode = "json_object"

    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in payload["additional_system_prompt"]
        return httpx.Response(200, json={"analysis": '{"root_cause":"OTHER"}'})

    with httpx.Client(transport=holmes_transport(handler)) as client:
        with pytest.raises(DomainError) as error:
            HolmesDiagnosisProvider(settings, client).diagnose({})
        assert error.value.code == "AI_INVALID_RESPONSE"


def test_planner_json_object_mode_keeps_action_whitelist(settings):
    from pydantic import SecretStr

    settings.planner_json_mode = "json_object"
    settings.planner_url = "https://api.deepseek.com/v1"
    settings.planner_model = "TEST/MOCK"
    settings.planner_api_key = SecretStr("TEST/MOCK")

    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "Schema" in payload["messages"][0]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"action_id":"DELETE_ALL"}'}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as error:
            LLMPlanner(settings, client).plan({})
        assert error.value.code == "BLOCKED"


@pytest.mark.parametrize(
    "info,code",
    [
        ({"version": "0.44.0", "toolsets": []}, "HOLMES_VERSION_MISMATCH"),
        ({"version": "0.45.0", "toolsets": [{"name": "bash", "enabled": True}]}, "BLOCKED"),
        ({"version": "0.45.0"}, "BLOCKED"),
    ],
)
def test_holmes_version_and_tool_permissions_fail_closed(settings, info, code):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=info)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as error:
            HolmesDiagnosisProvider(settings, client).diagnose({})
        assert error.value.code == code
        assert calls == ["GET"]


@pytest.mark.parametrize("mysql_error,verdict", [(2003, Verdict.FAIL), (1045, Verdict.UNKNOWN)])
def test_sql_refusal_and_authentication_are_distinct(settings, monkeypatch, mysql_error, verdict):
    def failed_connection(**kwargs):
        assert kwargs["host"] == "127.0.0.1" and kwargs["connect_timeout"] > 0
        raise pymysql.OperationalError(mysql_error, "TEST/MOCK")

    monkeypatch.setattr(pymysql, "connect", failed_connection)
    with httpx.Client() as client:
        assert Probes(settings, client).sql().verdict == verdict


def test_docker_forces_local_context_and_argument_array(settings, monkeypatch):
    captured = []

    def fake_run(arguments, **kwargs):
        captured.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout="[]", stderr="")

    monkeypatch.setenv("DOCKER_HOST", "tcp://production.invalid:2375")
    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerController(settings).snapshot()
    arguments, options = captured[0]
    assert arguments[:4] == ["docker", "--context", "default", "compose"]
    assert "aiops-demo" in arguments and options["shell"] is False
    assert "DOCKER_HOST" not in options["env"]


def test_verification_preserves_each_observation(settings, monkeypatch):
    settings.verification_attempts = 3
    samples = [
        [Probe(name=n, verdict=v, observed={}) for n in ("mysql_select_1", "app_db_check")]
        for v in (Verdict.FAIL, Verdict.UNKNOWN, Verdict.PASS)
    ]
    with httpx.Client() as client:
        probes = Probes(settings, client)
        monkeypatch.setattr(probes, "mysql", lambda: samples.pop(0))
        observed = []
        result = probes.verify(observed.append)
        assert result.verdict == Verdict.PASS
        assert len(observed) == len(result.samples) == 3
