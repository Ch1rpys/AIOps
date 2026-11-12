"""影响图与复盘摘要：全部从持久化记录生成，不现场重算。

见 docs/落地执行计划.md §7：
- 影响图必须区分「已观测异常 / 配置依赖 / 推断传播」三类关系；
- 启动顺序不等于业务因果，不能画成故障传播链；
- 复盘摘要来自已保存的工单记录，不得现场重跑 Docker 动作。
"""

from app.schemas.domain import now
from app.services.topology import build_impact_graph, load_topology

# 根因 → 受影响服务的映射只用于「推断传播」的标注，不参与判定。
ROOT_CAUSE_SERVICE = {
    "MYSQL_SERVICE_STOPPED": "demo-mysql",
    "REDIS_SERVICE_STOPPED": "demo-redis",
    "UPSTREAM_APP_STOPPED": "demo-app",
    "NGINX_UPSTREAM_MISCONFIGURED": "demo-nginx",
}


def evidence_payloads(item: dict) -> list[dict]:
    return [record["payload"] for record in item.get("records", []) if record["kind"] == "evidence"]


def audit_records(item: dict, kind: str) -> list[dict]:
    return [record for record in item.get("records", []) if record["kind"] == kind]


def last_container_state(item: dict) -> dict:
    states: dict = {}
    for payload in evidence_payloads(item):
        if payload.get("kind") == "container_state":
            states = payload.get("raw") or {}
    return states


def collected_probes(item: dict) -> list[dict]:
    """同名探针只保留最后一次观测。

    复采与受限补采都会把同一批探针再记一遍，
    全部堆在一起会出现「节点状态是 running 却标着异常」这种自相矛盾。
    """
    latest: dict[str, dict] = {}
    order: list[str] = []
    for payload in evidence_payloads(item):
        if payload.get("kind") != "probe":
            continue
        raw = payload.get("raw") or {}
        name = str(raw.get("name"))
        if name not in latest:
            order.append(name)
        latest[name] = raw
    return [latest[name] for name in order]


def service_of_evidence(topology, payload: dict) -> str | None:
    """判断一条证据属于哪个服务：探针按注册表归属，日志按服务名归属。"""
    kind = payload.get("kind")
    if kind == "logs":
        source = str(payload.get("source", ""))
        return source if source in topology.services else None
    if kind == "probe":
        name = (payload.get("raw") or {}).get("name")
        for service_id in topology.services:
            if name in topology.probes_of(service_id):
                return service_id
    return None


def incident_impact_graph(item: dict) -> dict:
    """从工单已保存的证据生成影响图。"""
    topology = load_topology()
    diagnosis = (item.get("detail") or {}).get("diagnosis") or {}
    graph = build_impact_graph(
        topology,
        last_container_state(item),
        collected_probes(item),
        diagnosis.get("root_cause"),
    )
    # §7.1：影响图要叠加探针、日志与诊断依据，而不只是节点与边。
    cited = set(diagnosis.get("evidence_ids") or [])
    logs: dict[str, list[str]] = {}
    citations: dict[str, list[str]] = {}
    for payload in evidence_payloads(item):
        service_id = service_of_evidence(topology, payload)
        if service_id is None:
            continue
        if payload.get("kind") == "logs":
            text = str((payload.get("raw") or {}).get("text", ""))
            tail = [line.strip() for line in text.splitlines() if line.strip()][-2:]
            logs.setdefault(service_id, []).extend(tail[-2:])
        if payload.get("id") in cited:
            citations.setdefault(service_id, []).append(str(payload.get("id")))
    for node in graph["nodes"]:
        node["diagnosis_evidence_ids"] = citations.get(node["id"], [])
        node["logs"] = logs.get(node["id"], [])[-2:]
    root_cause_service = ROOT_CAUSE_SERVICE.get(diagnosis.get("root_cause") or "")
    for node in graph["nodes"]:
        node["is_root_cause"] = bool(root_cause_service) and node["id"] == root_cause_service
    for edge in graph["edges"]:
        # 推断传播只在「沿业务边、且下游确实有实测异常」时成立。
        edge["inferred"] = bool(
            edge["kind"] == "inferred" and root_cause_service and edge["to"] == root_cause_service
        )
    detail = item.get("detail") or {}
    explanation = detail.get("topology_explanation")
    if isinstance(explanation, dict):
        explanation = {**explanation, "rule_ms": detail.get("rule_ms")}
    graph["rule_explanation"] = explanation
    if detail.get("diagnosis_source") == "registered_repair":
        graph["model_inference"] = {"root_cause": None, "service": None}
    else:
        graph["model_inference"] = {
            "root_cause": diagnosis.get("root_cause"),
            "service": root_cause_service,
        }
    graph["generated_at"] = now()
    graph["source"] = "incident_records"
    return graph


def build_review(item: dict) -> dict:
    """复盘摘要：何时发生、影响哪个接口、根因及引用、批准动作、修复前后探针、未解决项。"""
    detail = item.get("detail") or {}
    diagnosis = detail.get("diagnosis") or {}
    verification = detail.get("verification") or {}
    plan = detail.get("plan") or {}
    approvals = audit_records(item, "approval")
    executions = audit_records(item, "execution")
    graph = incident_impact_graph(item)
    impacted = [interface for interface in graph["interfaces"] if interface.get("impacted")]
    unresolved: list[str] = []
    if item.get("status") == "WAITING_APPROVAL":
        unresolved.append("计划已生成，等待人工审批")
    error = detail.get("error")
    if error:
        unresolved.append(f"流程错误：{error.get('code')} · {error.get('message')}")
    if verification.get("verdict") and verification["verdict"] != "PASS":
        unresolved.append(f"复测结论：{verification['verdict']}")
    if detail.get("supplement_stopped"):
        unresolved.append(f"补采未完成：{detail['supplement_stopped']}")
    for candidate in diagnosis.get("candidates") or []:
        missing = candidate.get("missing_checks") or []
        if missing:
            unresolved.append(f"候选 {candidate.get('root_cause')} 仍缺检查：{'、'.join(missing)}")
    before = collected_probes(item)
    after = [probe for sample in verification.get("samples") or [] for probe in sample]
    return {
        "incident_id": item.get("id"),
        "scenario": item.get("scenario_key"),
        "mode": item.get("mode"),
        "status": item.get("status"),
        "occurred_at": item.get("created_at"),
        "finished_at": item.get("finished_at"),
        "root_cause": diagnosis.get("root_cause"),
        "root_cause_summary": diagnosis.get("summary"),
        "root_cause_evidence_ids": diagnosis.get("evidence_ids") or [],
        "affected_interfaces": impacted,
        "approved_action": {
            "plan": plan or None,
            "approval": approvals[-1]["payload"] if approvals else None,
            "execution": executions[-1]["payload"] if executions else None,
        },
        "probes_before": before,
        "probes_after": after,
        "unresolved": unresolved,
        "impact_graph": graph,
        "audit_appendix": [
            {
                "id": record["id"],
                "kind": record["kind"],
                "created_at": record["created_at"],
            }
            for record in item.get("records", [])
        ],
        "twin_trial": detail.get("twin_trial"),
        "twin_certificate": detail.get("twin_certificate"),
        "recipe_title": detail.get("recipe_title"),
        "generated_at": now(),
        "source": "incident_records",
    }


def render_review_markdown(review: dict) -> str:
    """复盘摘要的 Markdown 导出：只投影历史记录，不含任何现场重算。"""
    lines = [
        f"# 复盘摘要 · {review.get('incident_id')}",
        "",
        f"场景：{review.get('scenario')} | 模式：{review.get('mode')} "
        f"| 状态：{review.get('status')}",
        f"发生时间：{review.get('occurred_at')} "
        f"| 结束时间：{review.get('finished_at') or '未结束'}",
        "",
        "> 这是历史回放，不是现场运行：本文由工单已保存的记录生成，不重新执行任何容器动作。",
        "",
        "## 根因与引用",
        "",
        f"- 根因：{review.get('root_cause') or '无有效诊断'}",
        f"- 结论摘要：{review.get('root_cause_summary') or '—'}",
        f"- 引用证据：{'、'.join(review.get('root_cause_evidence_ids') or []) or '—'}",
        "",
        "## 受影响接口",
        "",
    ]
    interfaces = review.get("affected_interfaces") or []
    if interfaces:
        for interface in interfaces:
            lines.append(
                f"- {interface.get('path')}（{interface.get('served_by')}）："
                f"{'、'.join(interface.get('blocked_by') or [])} · {interface.get('evidence')}"
            )
    else:
        lines.append("- 未观测到受影响的接口。")
    lines.extend(["", "## 批准动作", ""])
    approval = (review.get("approved_action") or {}).get("approval")
    plan = (review.get("approved_action") or {}).get("plan")
    execution = (review.get("approved_action") or {}).get("execution")
    if plan:
        lines.append(
            f"- 计划：{plan.get('action_id')} → {plan.get('target')}（{plan.get('risk')}）"
        )
    if approval:
        lines.append(
            f"- 审批：{approval.get('decision')} · {approval.get('operator') or '本地管理员'}"
        )
    if execution:
        lines.append(f"- 执行：{execution.get('state')}")
    if not (plan or approval or execution):
        lines.append("- 本次没有经过审批的执行动作。")
    lines.extend(["", "## 修复前后探针", ""])
    before = review.get("probes_before") or []
    after = review.get("probes_after") or []
    lines.append("修复前：")
    lines.extend(f"- {probe.get('name')}：{probe.get('verdict')}" for probe in before)
    if not before:
        lines.append("- 无采样")
    lines.append("修复后：")
    lines.extend(f"- {probe.get('name')}：{probe.get('verdict')}" for probe in after)
    if not after:
        # 没复测就说没复测：空着会被读成「都健康」。
        lines.append("- 无复测采样（本次没有走到复测）")
    certificate = review.get("twin_certificate") or {}
    trial = review.get("twin_trial") or {}
    lines.extend(["", "## 影子预测与证书", ""])
    if trial.get("skipped"):
        lines.append(f"- 影子不可用，本次没有反事实预测（{trial.get('reason') or 'skipped'}）。")
    elif trial:
        lines.append(
            f"- 影子预测将变绿：{'、'.join(trial.get('predicted_pass') or []) or '无'}。"
        )
        lines.append(
            f"- 影子预测仍红：{'、'.join(trial.get('predicted_still_fail') or []) or '无'}。"
        )
        if trial.get("would_resolve_signature"):
            lines.append("- 影子预测：这次可以完全恢复。")
        else:
            lines.append("- 影子预测：这次还不能完全恢复。")
    else:
        lines.append("- 本次没有影子试修记录。")
    if certificate:
        lines.append(f"- 证书 match_rate：{certificate.get('match_rate')}")
        surprise = certificate.get("surprise") or []
        if surprise:
            lines.append(f"- 与真环境不符的探针：{'、'.join(surprise)}")
        else:
            lines.append("- 预测与真环境复测一致。")
    score = review.get("score_json") or (review.get("evaluation") or {}).get("score_json")
    if isinstance(score, dict) and "weak" in str(score.get("parse") or ""):
        lines.append("- 未见计分使用了弱解析（从摘要/evidence_ids 提取探针），不是模型填写的 failing_check_ids。")
    lines.extend(["", "## 未解决项", ""])
    unresolved = review.get("unresolved") or []
    if unresolved:
        lines.extend(f"- {item}" for item in unresolved)
    else:
        lines.append("- 无。")
    lines.extend(["", "## 审计附录", ""])
    for record in review.get("audit_appendix") or []:
        lines.append(f"- {record.get('created_at')} · {record.get('kind')} · {record.get('id')}")
    lines.append("")
    return "\n".join(lines)


def render_talk_markdown(review: dict) -> str:
    """三分钟现场词：由工单记录生成，不把 holdout 原语写进讲稿。"""
    title = review.get("recipe_title") or review.get("scenario") or "编译器配方"
    trial = review.get("twin_trial") or {}
    certificate = review.get("twin_certificate") or {}
    if trial.get("skipped"):
        prediction = "影子不可用，本次没有反事实预测。"
    elif trial.get("would_resolve_signature"):
        prediction = "影子预测这次可以完全恢复。"
    elif trial:
        prediction = "影子预测这次还不能完全恢复。"
    else:
        prediction = "本次没有影子试修记录。"
    match = certificate.get("match_rate")
    if match is None:
        cert_line = "还没有证书。"
    else:
        cert_line = f"证书 match_rate={match}。对不上记预测失败，复测仍以真环境为准。"
    return "\n".join(
        [
            "# 三分钟现场词",
            "",
            "1. 打开 /compiler：池子里有一批哈希，holdout 被冻住，不要在演示里注入考题。",
            f"2. 工作台注入「{title}」。四个容器里有的停、有的断网，探针红的不只一条。",
            f"3. 诊断规划后先看影子：{prediction}批准后真环境复测与预测对照。{cert_line}",
            "4. 打开评测「未见故障」：抽 holdout。模型上下文没有配方名。展示签名覆盖，不报虚高枚举正确率。",
            "",
        ]
    )
