"""评测跑批的规模化验证：3 场景 × 3 轮 = 9 次盲诊，A/B 两臂共 18 条记录。

**这里用的是测试替身模型，不是真实模型**：它验证的是评测机制
（注入 → 固化快照 → 两臂共用快照 → 落库 → 汇总 → 报告），
不验证模型的真实能力。真实模型的 9 次盲诊需要凭据，见 docs/落地执行计划.md §14.2。
"""

from test_evaluation import runs, trigger


def test_nine_runs_across_three_scenarios_are_aggregated(client, world):
    for scenario in ("MYSQL_DOWN", "REDIS_DOWN", "NGINX_502"):
        for index in range(3):
            trigger(client, scenario=scenario, key=f"TEST-eval-{scenario}-{index}")
    rows = runs(client, limit=100)
    # 9 次实验 × 2 臂 = 18 条记录，全部落库可复查。
    assert len(rows) == 18
    assert {row["arm"] for row in rows} == {"A", "B"}
    # 每条记录都要有故障成立结论，否则不能进正确率分母。
    assert all(row["fault_present"] == 1 for row in rows)

    summary = client.get("/api/evaluation/summary").json()
    arms = {item["arm"]: item for item in summary["arms"]}
    assert set(arms) == {"A", "B"}
    for arm in arms.values():
        assert arm["runs"] == 9
        assert arm["valid_runs"] == 9
        assert arm["accuracy"] is not None
        assert arm["rejection_rate"] is not None
    # 测试替身模型只认 MYSQL 场景：另外两个场景判错，正确率因此不是 100%。
    # 这正是要验证的：聚合要能区分「判对」与「判错」，不能一律算对。
    assert arms["A"]["correct"] == 3
    assert arms["B"]["correct"] == 3
    assert arms["A"]["accuracy"] == round(1 / 3, 4)
    # 纯诊断对照不产生处置提议：这些指标必须显示「不适用」，不是 0。
    assert summary["proposal_metrics_applicable"] is False


def test_arms_of_the_same_run_share_one_snapshot(client, world):
    trigger(client, scenario="MYSQL_DOWN", arms=("A", "B"), key="TEST-eval-shared")
    rows = runs(client, limit=10)
    hashes = {row["snapshot_hash"] for row in rows}
    assert len(hashes) == 1
    assert rows[0]["snapshot_hash"]


def test_summary_timing_is_split_not_a_single_number(client, world):
    trigger(client, scenario="MYSQL_DOWN", key="TEST-eval-timing")
    summary = client.get("/api/evaluation/summary").json()
    timing = summary["arms"][0]["timing"]
    assert timing["model_ms"] is not None
    assert timing["collect_ms"] is not None
    assert timing["container_ms"] is not None
    assert timing["experiment_total_ms"] >= timing["collect_ms"]
    # 纯诊断没有审批环节：不适用，不是 0。
    assert timing["approval_ms"] is None
