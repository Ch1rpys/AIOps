"""盲诊评测跑批：3 场景 × 3 轮 = 9 次盲诊，逐条人工复核。

见 docs/落地执行计划.md §4.6 与 §9.3。
- 真值不进入模型上下文；人工复核时先独立判断、后揭晓真值，避免锚定；
- 失败与拒答的原始结论全部落库并写进报告，不美化；
- 报告显式声明：9 次是复现基线，不构成统计显著性；假设空间被根因枚举收窄。
"""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.schemas.domain import now  # noqa: E402

ROOT_CAUSES = [
    "MYSQL_SERVICE_STOPPED",
    "REDIS_SERVICE_STOPPED",
    "UPSTREAM_APP_STOPPED",
    "INSUFFICIENT_EVIDENCE",
    "OTHER",
]


def ask_root_cause(prompt: str) -> str:
    print(prompt)
    print("可选：" + " / ".join(ROOT_CAUSES))
    answer = input("你的独立判断：").strip()
    if answer not in ROOT_CAUSES:
        raise ValueError(f"无法识别的根因：{answer}")
    return answer


def summarize_evidence(detail: dict) -> str:
    """复核者看到的证据摘要：容器状态、探针观测与服务日志尾部。

    只给观测，不给真值，也不给场景注册的检查清单，
    否则人工复核会被答案锚定，失去独立判断的意义。
    """
    items = (detail.get("evidence") or {}).get("items") or []
    lines = []
    for item in items:
        if item.get("kind") == "container_state":
            states = ", ".join(
                f"{name}={value.get('state')}"
                for name, value in sorted((item.get("raw") or {}).items())
            )
            lines.append(f"容器状态：{states}")
        elif item.get("kind") == "probe":
            raw = item.get("raw") or {}
            observed = raw.get("observed") or {}
            detail_text = observed.get("error") or observed.get("status_code") or ""
            lines.append(
                f"探针 {raw.get('name')}：{raw.get('verdict')}"
                + (f" · {detail_text}" if detail_text else "")
            )
    for item in items:
        if item.get("kind") != "logs":
            continue
        text = str((item.get("raw") or {}).get("text", ""))
        tail = [line for line in text.splitlines() if line.strip()][-3:]
        if tail:
            lines.append(f"日志 {item.get('source')} 尾部：{tail[-1][:160]}")
    lines.append(f"证据条数：{len(items)}")
    return "\n".join(lines)


def render_markdown(transcript: dict, summary: dict | None = None) -> str:
    """评测报告：结论、消耗与人工复核结果全部保留，失败与拒答不美化。"""
    results = transcript.get("results", [])
    summary = summary or {}
    arms_text = "、".join(transcript.get("arms", []))
    lines = [
        "# 盲诊评测报告",
        "",
        f"- 开始时间：{transcript.get('started_at')}",
        f"- 模式：{transcript.get('mode')}",
        f"- 轮次：每场景 {transcript.get('rounds')} 轮，实验臂 {arms_text}",
        f"- 结论：{transcript.get('result', 'RUNNING')}",
        "",
        "## 诚实性声明",
        "",
    ]
    lines.extend(f"- {note}" for note in transcript.get("notes", []))
    lines.extend(
        [
            "",
            "## 各臂对照",
            "",
            "| 轮次 | 场景 | 臂 | 结论 | 证据充分 | 故障成立 | 判卷 | 拒答 "
            "| 模型调用 | 耗时(ms) | 人工复核 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in results:
        lines.append(
            "| {run_id} | {round} | {scenario} | {arm} | {root} | {sufficient} | {fault} "
            "| {correct} | {rejected} | {calls} | {latency} | {review} |".format(
                run_id=item.get("run_id"),
                round=item.get("round"),
                scenario=item.get("scenario"),
                arm=item.get("arm"),
                root=item.get("root_cause"),
                sufficient=item.get("evidence_sufficient"),
                fault=item.get("fault_present"),
                correct="正确" if item.get("correct") == 1 else "不正确",
                rejected="是" if item.get("rejected") == 1 else "否",
                calls=item.get("model_calls") if item.get("model_calls") is not None else "未知",
                latency=item.get("latency_ms") if item.get("latency_ms") is not None else "未知",
                review="一致" if item.get("reviewer_matches_truth") else "不一致",
            )
        )
    lines.extend(["", "## 耗时拆分（§9.2）", ""])
    lines.append("| 实验臂 | 模型时间 | 采证时间 | 容器停止 | 容器恢复 | 人工审批等待 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for arm in summary.get("arms", []):
        split = arm.get("timing") or {}
        lines.append(
            "| {arm} | {model} | {collect} | {container} | {restore} | {approval} |".format(
                arm=arm.get("arm"),
                model=split.get("model_ms"),
                collect=split.get("collect_ms"),
                container=split.get("container_ms"),
                restore=split.get("restore_ms"),
                approval="不适用",
            )
        )
    lines.extend(["", "## 端到端对照（闭环路径）", ""])
    end_to_end = summary.get("end_to_end") or {}
    lines.append(f"- 处置提议：{end_to_end.get('proposals', 0)} 条")
    lines.append(
        f"- 错误处置提议数：{end_to_end.get('illegal_proposals', 0)} 条"
        f"（{end_to_end.get('illegal_proposal_rate')}）"
    )
    blocked = end_to_end.get("blocked_proposals", 0)
    lines.append(f"- 拦截率：{end_to_end.get('block_rate')}（{blocked} 条被拦下）")
    lines.append(f"- 恢复成功率：{end_to_end.get('recovery_success_rate')}")
    lines.append(f"- {end_to_end.get('note', '')}")
    rejections = sum(1 for item in results if item.get("rejected") == 1)
    contradictions = sum(
        1
        for item in results
        if item.get("evidence_sufficient") is False and not item.get("rejected")
    )
    lines.extend(["", "## 结论分布", ""])
    lines.append(f"- 实验次数：{len(results)}")
    lines.append(f"- 拒答（INSUFFICIENT_EVIDENCE）：{rejections} 次")
    lines.append(f"- 自相矛盾（证据不足但给了确定根因）：{contradictions} 次")
    if rejections == 0:
        lines.append(
            "- **本次没有出现任何拒答**：如果输入里确实还有线索、或模型在过度自信，"
            "这本身就是要报告的问题，不能因为「全是确定答案」就当作好结果。"
        )
    lines.extend(["", "## 模型原始结论", ""])
    for item in results:
        lines.extend(
            [
                f"### {item.get('scenario')} · 第 {item.get('round')} 轮 "
                f"· 实验臂 {item.get('arm')}",
                "",
                "```json",
                json.dumps(item.get("diagnosis"), ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def run(rounds: int, arms: list[str], base_url: str, timeout_s: int) -> int:
    settings = Settings()
    token = settings.admin_token.get_secret_value()
    if not token:
        print("FAIL 未配置 ADMIN_TOKEN，评测接口需要管理员令牌")
        return 1
    path = settings.data_dir / "evaluation" / f"blind-{uuid4().hex}.json"
    report_path = path.with_suffix(".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    transcript = {
        "mode": "REAL",
        "started_at": now(),
        "rounds": rounds,
        "arms": arms,
        "results": [],
        "notes": [
            "9 次实验是复现基线，不足以证明广泛泛化或统计显著性。",
            "根因枚举收窄了假设空间：评测只能在这五种表述里选择。",
            "纯诊断对照不产生处置提议，错误处置提议数与拦截率不适用。",
        ],
    }

    def save():
        path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(render_markdown(transcript, summary), encoding="utf-8")

    with httpx.Client(
        base_url=base_url,
        timeout=60,
        trust_env=False,
        headers={"X-Admin-Token": token},
    ) as client:

        def call(method, url, body=None, key=None):
            headers = {"Idempotency-Key": key} if key else {}
            response = client.request(method, url, json=body, headers=headers)
            response.raise_for_status()
            return response.json()

        def rows(view: bool = False):
            suffix = "?view=evaluation" if view else ""
            return call("GET", f"/api/evaluation/runs{suffix}")

        try:
            health = call("GET", "/api/health")
            if health["mode"] != "REAL":
                raise ValueError("评测拒绝 TEST/MOCK 模式")
            if not health["lab"]["healthy"]:
                raise ValueError("demo 基线未全部通过，禁止评测")
            if not health["holmes"]["status"] == "PASS":
                raise ValueError("HolmesGPT 服务未就绪，无法进行真实模型评测")
            summary = call("GET", "/api/evaluation/summary")
            print("PASS REAL 基线与模型服务预检")
            for scenario in ("MYSQL_DOWN", "REDIS_DOWN", "NGINX_502"):
                for index in range(rounds):
                    before = {row["id"] for row in rows()}
                    call(
                        "POST",
                        "/api/evaluation/runs",
                        {"scenario": scenario, "arms": arms, "confirm": True},
                        "eval-" + uuid4().hex,
                    )
                    deadline = time.monotonic() + timeout_s
                    fresh = []
                    while time.monotonic() < deadline:
                        fresh = [row for row in rows() if row["id"] not in before]
                        if len(fresh) >= len(arms):
                            break
                        time.sleep(2)
                    if len(fresh) < len(arms):
                        raise ValueError(f"{scenario} 第 {index + 1} 轮评测超时")
                    for row in fresh:
                        detail = call("GET", f"/api/evaluation/runs/{row['id']}?view=evaluation")
                        print("\n" + "=" * 72)
                        print(
                            f"{scenario} · 第 {index + 1} 轮 "
                            f"· 实验臂 {row['arm']} · {row['status']}"
                        )
                        print(summarize_evidence(detail))
                        print("模型结论：", json.dumps(detail.get("diagnosis"), ensure_ascii=False))
                        reviewer = ask_root_cause("请依据上面的证据独立判断根因：")
                        truth = detail.get("truth_scenario")
                        truth_cause = {
                            "MYSQL_DOWN": "MYSQL_SERVICE_STOPPED",
                            "REDIS_DOWN": "REDIS_SERVICE_STOPPED",
                            "NGINX_502": "UPSTREAM_APP_STOPPED",
                        }[truth]
                        entry = {
                            "run_id": row["id"],
                            "arm": row["arm"],
                            "scenario": truth,
                            "round": index + 1,
                            "status": row["status"],
                            "root_cause": row["root_cause"],
                            "evidence_sufficient": row["evidence_sufficient"],
                            "fault_present": row["fault_present"],
                            "correct": row.get("correct"),
                            "rejected": row.get("rejected"),
                            "model_calls": row["model_calls"],
                            "latency_ms": row["latency_ms"],
                            "token_usage": row["token_usage"],
                            "snapshot_hash": row["snapshot_hash"],
                            "reviewer_root_cause": reviewer,
                            "reviewer_matches_truth": reviewer == truth_cause,
                            "reviewer_matches_conclusion": reviewer == row["root_cause"],
                            "diagnosis": detail.get("diagnosis"),
                        }
                        transcript["results"].append(entry)
                        summary = call("GET", "/api/evaluation/summary")
                        save()
                        print(f"系统判卷：{'正确' if row.get('correct') == 1 else '不正确'}")
                        agreement = "一致" if entry["reviewer_matches_truth"] else "不一致"
                        print(f"人工复核：与真值{agreement}")
                    print(f"\n完成 {scenario} 第 {index + 1} 轮")
            transcript["result"] = "PASS"
            save()
            print("\n" + "=" * 72)
            print(f"盲诊评测完成，记录：{path}")
            print(f"评测报告：{report_path}")
            print(json.dumps(transcript["notes"], ensure_ascii=False, indent=2))
            return 0
        except (httpx.HTTPError, ValueError, KeyError, EOFError) as error:
            transcript["result"] = "FAIL"
            transcript["reason"] = str(error)
            save()
            print(f"FAIL {error}")
            print(f"证据：{path}")
            return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="盲诊评测跑批：逐条人工复核，不使用 Mock")
    parser.add_argument("--rounds", type=int, default=3, help="每个场景的轮次，默认 3")
    parser.add_argument("--arms", default="A,B", help="参与对照的实验臂，默认 A,B")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=180, help="单次实验等待秒数")
    arguments = parser.parse_args()
    sys.exit(
        run(
            arguments.rounds,
            [item.strip() for item in arguments.arms.split(",") if item.strip()],
            arguments.base_url,
            arguments.timeout,
        )
    )
