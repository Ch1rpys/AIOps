"""真实场景命令入口；审批命令仅在人工明确决定后调用。"""

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
from app.schemas.domain import Scenario  # noqa: E402


def run(args) -> int:
    settings = Settings()
    folder = settings.data_dir / "real-runs"
    folder.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url="http://127.0.0.1:8000",
        timeout=45,
        trust_env=False,
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as client:

        def call(method, path, body=None):
            response = client.request(
                method,
                path,
                json=body,
                headers={"Idempotency-Key": "real-" + uuid4().hex} if method == "POST" else {},
            )
            response.raise_for_status()
            return response.json()

        if args.command == "diagnose":
            baseline = call("GET", "/api/lab/status")
            if not baseline["healthy"] or baseline["active_incident_id"]:
                raise ValueError("基线未就绪或存在未结束实验")
            created = call("POST", f"/api/scenarios/{args.scenario}/inject", {"confirm": True})
            incident_id = created["incident_id"]
            print("PASS REAL INJECTED", incident_id, flush=True)
            call("POST", f"/api/incidents/{incident_id}/diagnose")
        else:
            incident_id = args.incident
            current = call("GET", f"/api/incidents/{incident_id}")
            if current["detail"].get("plan_id") != args.plan_id:
                raise ValueError("审批计划 ID 不匹配")
            call(
                "POST",
                f"/api/incidents/{incident_id}/approval",
                {"plan_id": args.plan_id, "decision": args.decision, "comment": args.comment},
            )
        for _ in range(150):
            item = call("GET", f"/api/incidents/{incident_id}")
            if item["status"] in {
                "WAITING_APPROVAL",
                "RESOLVED",
                "FAILED",
                "ERROR",
                "UNKNOWN",
                "BLOCKED",
                "CANCELLED",
            }:
                break
            time.sleep(2)
        (folder / f"{incident_id}.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        response = client.get(f"/api/incidents/{incident_id}/report.md")
        response.raise_for_status()
        (folder / f"{incident_id}.md").write_text(response.text, encoding="utf-8")
        print(
            json.dumps(
                {
                    "id": item["id"],
                    "scenario_key": item["scenario_key"],
                    "mode": item["mode"],
                    "status": item["status"],
                    "plan_id": item["detail"].get("plan_id"),
                    "plan": item["detail"].get("plan"),
                    "verification": item["detail"].get("verification"),
                    "error": item["detail"].get("error"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        expected = (
            {"WAITING_APPROVAL"}
            if args.command == "diagnose"
            else ({"RESOLVED"} if args.decision == "approve" else {"CANCELLED"})
        )
        return 0 if item["mode"] == "REAL" and item["status"] in expected else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    diagnose = sub.add_parser("diagnose")
    diagnose.add_argument("scenario", choices=[s.value for s in Scenario])
    approval = sub.add_parser("approval")
    approval.add_argument("incident")
    approval.add_argument("plan_id")
    approval.add_argument("decision", choices=["approve", "reject"])
    approval.add_argument("--comment", required=True)
    try:
        sys.exit(run(parser.parse_args()))
    except (httpx.HTTPError, ValueError) as error:
        print(f"FAIL {type(error).__name__}: {error}")
        sys.exit(1)
