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


def run(preflight: bool) -> int:
    settings = Settings()
    transcript = {"mode": "REAL", "started_at": now(), "checks": []}
    path = settings.data_dir / "acceptance" / f"mysql-{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    def save():
        path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(
        base_url="http://127.0.0.1:8000",
        timeout=45,
        trust_env=False,
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as client:

        def call(method, url, body=None, key=None):
            headers = {"Idempotency-Key": key} if key else {}
            response = client.request(method, url, json=body, headers=headers)
            transcript["checks"].append(
                {
                    "at": now(),
                    "method": method,
                    "url": url,
                    "status": response.status_code,
                    "body": response.json(),
                }
            )
            save()
            response.raise_for_status()
            return response.json()

        try:
            health = call("GET", "/api/health")
            if health["mode"] != "REAL":
                raise ValueError("验收拒绝 TEST/MOCK 模式")
            if not health["lab"]["healthy"] or health["holmes"]["status"] != "PASS":
                raise ValueError("Docker / demo 基线 / Holmes 服务未全部就绪")
            if not health["planner"]["configured"] or not settings.admin_token.get_secret_value():
                raise ValueError("Planner / 管理员令牌未配置")
            print("PASS REAL 基线与服务预检（尚未验证真实模型调用）")
            if preflight:
                transcript["result"] = "PREFLIGHT_PASS"
                save()
                return 0
            if input("输入 INJECT MYSQL_DOWN 确认停止隔离数据库：").strip() != "INJECT MYSQL_DOWN":
                raise ValueError("用户未确认注入")
            result = call(
                "POST",
                "/api/scenarios/MYSQL_DOWN/inject",
                {"confirm": True},
                "accept-inject-" + uuid4().hex,
            )
            incident_id = result["incident_id"]
            if result["status"] != "INJECTED" or not result["fault_confirmed"]:
                raise ValueError("故障未被真实双探针确认")
            print("PASS REAL 停止 MySQL 并确认双探针失败", incident_id)
            call("POST", f"/api/incidents/{incident_id}/diagnose")
            for _ in range(180):
                item = call("GET", f"/api/incidents/{incident_id}")
                if item["status"] == "WAITING_APPROVAL":
                    plan = item["detail"]["plan"]
                    print(json.dumps(plan, ensure_ascii=False, indent=2))
                    decision = (
                        "approve"
                        if input("输入 APPROVE 批准此计划，其他输入拒绝：").strip() == "APPROVE"
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
                elif item["status"] in {
                    "RESOLVED",
                    "CANCELLED",
                    "ERROR",
                    "FAILED",
                    "UNKNOWN",
                    "BLOCKED",
                }:
                    report = call("GET", f"/api/incidents/{incident_id}/report")
                    if (
                        report["status"] != "RESOLVED"
                        or report["detail"]["verification"]["verdict"] != "PASS"
                    ):
                        raise ValueError(f"闭环终止于 {report['status']}")
                    transcript["result"] = "PASS"
                    save()
                    print("PASS REAL MYSQL_DOWN 诊断、人工审批、恢复、独立复测、记录查询")
                    print(path)
                    return 0
                time.sleep(2)
            raise ValueError("工作流等待超时，请查看工单，禁止盲目重试注入")
        except (httpx.HTTPError, ValueError, KeyError, EOFError) as error:
            transcript["result"] = "FAIL"
            transcript["reason"] = str(error)
            save()
            print(f"FAIL {error}")
            print(f"证据：{path}")
            return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实 MYSQL_DOWN 人工审批验收，不使用 Mock")
    parser.add_argument("--preflight", action="store_true")
    sys.exit(run(parser.parse_args().preflight))
