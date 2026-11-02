"""真实模型闭环验收：场景 × 轮次可参数化，逐轮人工审批。

见 docs/落地执行计划.md §3.2：
- 三场景各 3 轮 = 9 次真实闭环，每轮都要人工审批；
- 每轮保存工单、审计记录、Markdown 报告与模型原始结论；
- 某一轮失败就保留失败工单，不重跑到全绿再展示。
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

SCENARIOS = ("MYSQL_DOWN", "REDIS_DOWN", "NGINX_502")
CONFIRM_WORDS = {
    "MYSQL_DOWN": "INJECT MYSQL_DOWN",
    "REDIS_DOWN": "INJECT REDIS_DOWN",
    "NGINX_502": "INJECT NGINX_502",
}
TERMINAL = {"RESOLVED", "CANCELLED", "ERROR", "FAILED", "UNKNOWN", "BLOCKED"}


def extract_diagnosis(item: dict) -> dict | None:
    """从审计记录里取出模型原始结论（不做美化，不改写）。"""
    for record in reversed(item.get("records", [])):
        if record.get("kind") != "step":
            continue
        phase = (record.get("payload") or {}).get("phase")
        output = (record.get("payload") or {}).get("output") or {}
        if phase == "DIAGNOSING" and output.get("diagnosis"):
            return output["diagnosis"]
    return (item.get("detail") or {}).get("diagnosis")


def run(args) -> int:
    settings = Settings()
    token = settings.admin_token.get_secret_value()
    folder = settings.data_dir / "acceptance"
    folder.mkdir(parents=True, exist_ok=True)
    transcript = {
        "mode": "REAL",
        "started_at": now(),
        "scenarios": args.scenario,
        "rounds": args.rounds,
        "runs": [],
    }
    path = folder / f"acceptance-{uuid4().hex}.json"

    def save():
        path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(
        base_url=args.base_url,
        timeout=45,
        trust_env=False,
        headers={"X-Admin-Token": token},
    ) as client:

        def call(method, url, body=None, key=None):
            headers = {"Idempotency-Key": key} if key else {}
            response = client.request(method, url, json=body, headers=headers)
            transcript.setdefault("http", []).append(
                {
                    "at": now(),
                    "method": method,
                    "url": url,
                    "status": response.status_code,
                    "body": response.json() if response.content else None,
                }
            )
            save()
            response.raise_for_status()
            return response.json()

        def call_text(method, url):
            response = client.request(method, url)
            transcript.setdefault("http", []).append(
                {"at": now(), "method": method, "url": url, "status": response.status_code}
            )
            save()
            response.raise_for_status()
            return response.text

        try:
            health = call("GET", "/api/health")
            if health["mode"] != "REAL":
                raise ValueError("验收拒绝 TEST/MOCK 模式")
            if not health["lab"]["healthy"] or health["holmes"]["status"] != "PASS":
                raise ValueError("Docker / demo 基线 / Holmes 服务未全部就绪")
            if not health["planner"]["configured"] or not token:
                raise ValueError("Planner / 管理员令牌未配置")
            print("PASS REAL 基线与服务预检（尚未验证真实模型调用）")
            if args.preflight:
                transcript["result"] = "PREFLIGHT_PASS"
                save()
                return 0

            for scenario in args.scenario:
                for index in range(args.rounds):
                    word = CONFIRM_WORDS[scenario]
                    if (
                        input(f"输入 {word} 确认注入 {scenario}（第 {index + 1} 轮）：").strip()
                        != word
                    ):
                        raise ValueError("用户未确认注入")
                    result = call(
                        "POST",
                        f"/api/scenarios/{scenario}/inject",
                        {"confirm": True},
                        "accept-inject-" + uuid4().hex,
                    )
                    incident_id = result["incident_id"]
                    if result["status"] != "INJECTED" or not result["fault_confirmed"]:
                        raise ValueError("故障未被真实探针确认")
                    print(f"PASS REAL 注入 {scenario} 并确认故障", incident_id)
                    call("POST", f"/api/incidents/{incident_id}/diagnose")
                    for _ in range(180):
                        item = call("GET", f"/api/incidents/{incident_id}")
                        if item["status"] == "WAITING_APPROVAL":
                            plan = item["detail"]["plan"]
                            print(json.dumps(plan, ensure_ascii=False, indent=2))
                            decision = (
                                "approve"
                                if input("输入 APPROVE 批准此计划，其他输入拒绝：").strip()
                                == "APPROVE"
                                else "reject"
                            )
                            call(
                                "POST",
                                f"/api/incidents/{incident_id}/approval",
                                {
                                    "plan_id": item["detail"]["plan_id"],
                                    "decision": decision,
                                    "comment": "真实命令行人工审批",
                                },
                                "accept-approval-" + uuid4().hex,
                            )
                        elif item["status"] in TERMINAL:
                            markdown = call_text("GET", f"/api/incidents/{incident_id}/report.md")
                            report_path = folder / f"{incident_id}.md"
                            report_path.write_text(markdown, encoding="utf-8")
                            diagnosis = extract_diagnosis(item)
                            # 审计记录一并导出：每轮的采集、模型、审批、执行都要能复查。
                            records_path = folder / f"{incident_id}.records.json"
                            records_path.write_text(
                                json.dumps(item.get("records", []), ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            diagnosis_path = folder / f"{incident_id}.diagnosis.json"
                            diagnosis_path.write_text(
                                json.dumps(diagnosis, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            # 模型原始输出单独留痕：模型说了什么，而不是我们解析后的结论。
                            raw_path = folder / f"{incident_id}.model-output.json"
                            raw_path.write_text(
                                json.dumps(
                                    {
                                        "raw_model_output": (item["detail"] or {}).get(
                                            "raw_model_output"
                                        ),
                                        "model_usage": (item["detail"] or {}).get("model_usage"),
                                        "diagnosis": diagnosis,
                                    },
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                                encoding="utf-8",
                            )
                            entry = {
                                "scenario": scenario,
                                "round": index + 1,
                                "incident_id": incident_id,
                                "status": item["status"],
                                "diagnosis": diagnosis,
                                "report_path": str(report_path),
                                "diagnosis_path": str(diagnosis_path),
                                "records_path": str(records_path),
                                "model_output_path": str(raw_path),
                            }
                            transcript["runs"].append(entry)
                            save()
                            if (
                                item["status"] != "RESOLVED"
                                or item["detail"]["verification"]["verdict"] != "PASS"
                            ):
                                # 失败工单保留，不重跑到全绿。
                                transcript["result"] = "FAIL"
                                transcript["reason"] = (
                                    f"{scenario} 第 {index + 1} 轮终止于 {item['status']}"
                                )
                                save()
                                print(f"FAIL {transcript['reason']}")
                                print(f"失败工单：{incident_id}")
                                print(f"证据：{path}")
                                return 1
                            print(f"PASS REAL {scenario} 第 {index + 1} 轮闭环通过", incident_id)
                            break
                        time.sleep(2)
                    else:
                        raise ValueError("工作流等待超时，请查看工单，禁止盲目重试注入")
            transcript["result"] = "PASS"
            save()
            print("PASS REAL 全部场景与轮次闭环通过")
            print(path)
            return 0
        except (httpx.HTTPError, ValueError, KeyError, EOFError) as error:
            transcript["result"] = "FAIL"
            transcript["reason"] = str(error)
            save()
            print(f"FAIL {error}")
            print(f"证据：{path}")
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型闭环验收，不使用 Mock")
    parser.add_argument(
        "--scenario",
        nargs="+",
        choices=[*SCENARIOS, "all"],
        default=list(SCENARIOS),
        help="要验收的场景，默认三个场景都跑",
    )
    parser.add_argument("--rounds", type=int, default=1, help="每个场景的轮次，默认 1")
    parser.add_argument("--preflight", action="store_true", help="只做预检，不注入故障")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    if "all" in arguments.scenario:
        arguments.scenario = list(SCENARIOS)
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
