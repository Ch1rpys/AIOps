"""评测跑批脚本的报告与人工复核证据（docs/落地执行计划.md §4.6 / §9.3）。

复核必须能独立判断：给复核者的证据里不能出现真值，也不能出现场景注册的检查清单。
"""

import importlib.util
import json

from app.config import ROOT

spec = importlib.util.spec_from_file_location("run_evaluation", ROOT / "scripts/run_evaluation.py")
run_evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_evaluation)

TRANSCRIPT = {
    "mode": "REAL",
    "started_at": "2026-11-02T10:00:00+00:00",
    "rounds": 3,
    "arms": ["A", "B"],
    "result": "PASS",
    "notes": ["样本量是复现基线，不构成统计显著性。"],
    "results": [
        {
            "run_id": "evalrun_1",
            "arm": "A",
            "scenario": "MYSQL_DOWN",
            "round": 1,
            "status": "COMPLETED",
            "root_cause": "MYSQL_SERVICE_STOPPED",
            "evidence_sufficient": True,
            "fault_present": 1,
            "correct": 1,
            "rejected": 0,
            "model_calls": 1,
            "latency_ms": 1200,
            "token_usage": None,
            "snapshot_hash": "abc123",
            "reviewer_root_cause": "MYSQL_SERVICE_STOPPED",
            "reviewer_matches_truth": True,
            "reviewer_matches_conclusion": True,
            "diagnosis": {"root_cause": "MYSQL_SERVICE_STOPPED"},
        },
        {
            "run_id": "evalrun_2",
            "arm": "B",
            "scenario": "MYSQL_DOWN",
            "round": 1,
            "status": "COMPLETED",
            "root_cause": "INSUFFICIENT_EVIDENCE",
            "evidence_sufficient": False,
            "fault_present": 1,
            "correct": 0,
            "rejected": 1,
            "model_calls": 2,
            "latency_ms": 2400,
            "token_usage": None,
            "snapshot_hash": "abc123",
            "reviewer_root_cause": "MYSQL_SERVICE_STOPPED",
            "reviewer_matches_truth": True,
            "reviewer_matches_conclusion": False,
            "diagnosis": {"root_cause": "INSUFFICIENT_EVIDENCE"},
        },
    ],
}

SUMMARY = {
    "arms": [
        {
            "arm": "A",
            "accuracy": 1.0,
            "timing": {
                "model_ms": 900,
                "collect_ms": 120,
                "container_ms": 30,
                "restore_ms": 40,
                "approval_ms": None,
            },
        },
        {
            "arm": "B",
            "accuracy": 0.0,
            "timing": {
                "model_ms": 1800,
                "collect_ms": 120,
                "container_ms": 30,
                "restore_ms": 40,
                "approval_ms": None,
            },
        },
    ],
    "end_to_end": {
        "proposals": 1,
        "illegal_proposals": 0,
        "illegal_proposal_rate": 0.0,
        "blocked_proposals": 0,
        "block_rate": None,
        "recovery_success_rate": 1.0,
        "note": "端到端指标只统计闭环路径真实产生的处置提议；纯诊断对照不适用。",
    },
}


def test_report_contains_honesty_statements_and_all_runs():
    report = run_evaluation.render_markdown(TRANSCRIPT, SUMMARY)
    assert "复现基线" in report
    assert "统计显著性" in report
    # 失败与拒答的原始结论都要在报告里，不美化。
    assert "INSUFFICIENT_EVIDENCE" in report
    assert "MYSQL_SERVICE_STOPPED" in report
    assert "evalrun_1" in report and "evalrun_2" in report
    assert "## 耗时拆分" in report
    assert "## 端到端对照（闭环路径）" in report
    assert "不适用" in report


def test_review_evidence_shows_observations_but_not_the_truth():
    detail = {
        "evidence": {
            "items": [
                {
                    "kind": "container_state",
                    "source": "docker-compose",
                    "raw": {"demo-mysql": {"state": "exited"}, "demo-app": {"state": "running"}},
                },
                {
                    "kind": "probe",
                    "source": "mysql_select_1",
                    "raw": {"name": "mysql_select_1", "verdict": "FAIL", "observed": {}},
                },
                {
                    "kind": "logs",
                    "source": "demo-mysql",
                    "raw": {"text": "line1\nline2 connection refused"},
                },
            ]
        }
    }
    text = run_evaluation.summarize_evidence(detail)
    assert "demo-mysql=exited" in text
    assert "mysql_select_1：FAIL" in text
    assert "connection refused" in text
    # 不能出现真值或场景注册信息，否则人工复核会被锚定。
    for token in ("MYSQL_DOWN", "MYSQL_SERVICE_STOPPED", "START_DEMO_MYSQL"):
        assert token not in text


def test_report_renders_without_summary():
    report = run_evaluation.render_markdown(TRANSCRIPT)
    assert "复现基线" in report
    assert json.dumps(TRANSCRIPT["results"][0], ensure_ascii=False)
