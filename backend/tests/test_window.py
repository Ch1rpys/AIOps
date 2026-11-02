"""M5：连续健康窗口与复发提示（docs/落地执行计划.md §8）。

这是唯一会改变 PASS 判定的切片，因此：
- 窗口默认关闭时，行为必须与现状逐字一致；
- 契约只加可选字段，不加枚举值；
- 失败采样不能被删除，UNKNOWN ≠ FAIL；
- 窗口内健康不能被宣称为「永久不复发」。
"""

import pytest
from app.config import Settings
from app.integrations.probes import Probes
from app.scenarios import SCENARIOS
from app.schemas.domain import Probe, Scenario, Verdict, Verification
from conftest import injected


def sample(verdict: Verdict) -> list[Probe]:
    return [
        Probe(name=name, verdict=verdict, observed={"mode": "TEST/MOCK"})
        for name in SCENARIOS[Scenario.MYSQL_DOWN].checks
    ]


def window_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        demo_mysql_password="private",
        verification_interval=0,
    )
    settings.verification_window_enabled = True
    settings.verification_window_samples = 5
    settings.verification_required_passes = 3
    settings.verification_window_seconds = 0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def test_window_is_disabled_by_default(settings, client, monkeypatch):
    """默认关闭：不产出 window 字段，判定与现状逐字一致。"""
    probes = Probes(settings, client)
    monkeypatch.setattr(probes, "for_scenario", lambda scenario: sample(Verdict.PASS))
    result = probes.verify(lambda items: None)
    assert result.window is None
    assert result.verdict == Verdict.PASS
    assert len(result.samples) == 1


def test_window_requires_consecutive_passes(tmp_path, client, monkeypatch):
    settings = window_settings(tmp_path)
    probes = Probes(settings, client)
    monkeypatch.setattr(probes, "for_scenario", lambda scenario: sample(Verdict.PASS))
    result = probes.verify(lambda items: None)
    assert result.window is not None
    assert result.window["enabled"] is True
    assert result.window["consecutive_passes"] == 3
    assert result.window["stable"] is True
    assert result.window["instability"] is False
    assert result.verdict == Verdict.PASS


def test_window_marks_instability_and_keeps_failed_samples(tmp_path, client, monkeypatch):
    settings = window_settings(
        tmp_path, verification_window_samples=4, verification_required_passes=2
    )
    probes = Probes(settings, client)
    sequence = [
        sample(Verdict.PASS),
        sample(Verdict.FAIL),
        sample(Verdict.PASS),
        sample(Verdict.PASS),
    ]
    monkeypatch.setattr(probes, "for_scenario", lambda scenario: sequence.pop(0))
    seen: list[list[Probe]] = []
    result = probes.verify(seen.append)
    assert result.window["instability"] is True
    assert result.window["failures"] == 1
    assert result.window["stable"] is False
    # 失败采样没有被删除，且逐次落盘。
    assert len(result.samples) == 4
    assert len(seen) == 4
    assert result.samples[1][0].verdict == Verdict.FAIL
    # verdict 仍是三值语义，不新增枚举。
    assert result.verdict in {Verdict.PASS, Verdict.FAIL, Verdict.UNKNOWN}


def test_unknown_is_not_a_failure(tmp_path, client, monkeypatch):
    settings = window_settings(
        tmp_path, verification_window_samples=3, verification_required_passes=2
    )
    probes = Probes(settings, client)
    sequence = [
        sample(Verdict.PASS),
        sample(Verdict.UNKNOWN),
        sample(Verdict.UNKNOWN),
    ]
    monkeypatch.setattr(probes, "for_scenario", lambda scenario: sequence.pop(0))
    result = probes.verify(lambda items: None)
    assert result.window["failures"] == 0
    assert result.window["instability"] is False
    assert result.window["consecutive_passes"] == 0
    assert result.verdict == Verdict.UNKNOWN


def test_verification_window_field_is_optional():
    plain = Verification(verdict=Verdict.PASS, samples=[[]])
    assert plain.window is None
    with pytest.raises(ValueError):
        Verification(verdict=Verdict.PASS, samples=[[]], window="not-a-dict")


def test_unstable_recovery_is_not_reported_as_resolved(client, world):
    """窗口内再次失败：不能算已恢复。"""
    world.window_enabled = True
    world.window_instability = True
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = client.get(f"/api/incidents/{incident_id}").json()
    plan_id = item["detail"]["plan_id"]
    client.post(
        f"/api/incidents/{incident_id}/approval",
        json={"plan_id": plan_id, "decision": "approve", "comment": "TEST/MOCK"},
        headers={"Idempotency-Key": "TEST-approve-window"},
    )
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] != "RESOLVED"
    assert item["detail"]["verification"]["window"]["instability"] is True


def test_stable_window_keeps_the_existing_resolution_path(client, world):
    """窗口开启但没有失败：判定与现状一致。"""
    world.window_enabled = True
    incident_id = injected(client)
    client.post(f"/api/incidents/{incident_id}/diagnose")
    item = client.get(f"/api/incidents/{incident_id}").json()
    plan_id = item["detail"]["plan_id"]
    client.post(
        f"/api/incidents/{incident_id}/approval",
        json={"plan_id": plan_id, "decision": "approve", "comment": "TEST/MOCK"},
        headers={"Idempotency-Key": "TEST-approve-stable"},
    )
    item = client.get(f"/api/incidents/{incident_id}").json()
    assert item["status"] == "RESOLVED"
    assert item["detail"]["verification"]["window"]["stable"] is True
