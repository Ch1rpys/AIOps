"""schema 版本与迁移护栏。

见 docs/落地执行计划.md §4.1：新增评测表必须显式迁移到 user_version=2，
且不得把版本号无条件写回 1 —— 那会让版本护栏失效、旧二进制照跑新 schema。
"""

import pytest
from app.config import DomainError
from app.repositories.store import SCHEMA_VERSION, Store


def tables_of(store):
    with store.engine.begin() as connection:
        return {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def version_of(store):
    with store.engine.begin() as connection:
        return connection.exec_driver_sql("PRAGMA user_version").scalar_one()


def test_new_database_is_created_at_current_version(tmp_path):
    store = Store(tmp_path / "fresh.db")
    assert version_of(store) == SCHEMA_VERSION == 3
    assert {"incidents", "audit_records", "evaluation_runs", "evaluation_arms"} <= tables_of(store)
    store.close()


def test_v1_database_upgrades_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(path)
    with store.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE evaluation_arms")
        connection.exec_driver_sql("DROP TABLE evaluation_runs")
        connection.exec_driver_sql("PRAGMA user_version=1")
        connection.exec_driver_sql(
            "INSERT INTO incidents (id, scenario_key, mode, status, created_at, updated_at, detail)"
            " VALUES ('inc_legacy', 'MYSQL_DOWN', 'REAL', 'CREATED', 't0', 't0', '{}')"
        )
    store.close()

    upgraded = Store(path)
    assert version_of(upgraded) == 3
    assert {"evaluation_runs", "evaluation_arms"} <= tables_of(upgraded)
    with upgraded.engine.begin() as connection:
        rows = connection.exec_driver_sql("SELECT id FROM incidents").fetchall()
    assert [row[0] for row in rows] == ["inc_legacy"]
    upgraded.close()


def test_version_never_downgrades_on_reopen(tmp_path):
    path = tmp_path / "reopen.db"
    first = Store(path)
    assert version_of(first) == 3
    first.close()
    second = Store(path)
    assert version_of(second) == 3
    second.close()


def test_v2_database_gains_the_cohort_column(tmp_path):
    path = tmp_path / "v2.db"
    store = Store(path)
    with store.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE evaluation_runs DROP COLUMN cohort")
        connection.exec_driver_sql(
            "INSERT INTO evaluation_runs ("
            "id, arm, truth_scenario, snapshot_hash, evidence_json, diagnosis_json, "
            "correct, rejected, fault_present, model_calls, latency_ms, token_usage, created_at"
            ") VALUES ("
            "'eval_old', 'A', 'MYSQL_DOWN', 'abc', '{}', '{}', 1, 0, 1, 1, 10, NULL, 't0'"
            ")"
        )
        connection.exec_driver_sql("PRAGMA user_version=2")
    store.close()
    upgraded = Store(path)
    assert version_of(upgraded) == 3
    with upgraded.engine.begin() as connection:
        row = connection.exec_driver_sql(
            "SELECT cohort FROM evaluation_runs WHERE id='eval_old'"
        ).one()
    assert row[0] == "standard"
    upgraded.close()


def test_unknown_version_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    store = Store(path)
    with store.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=99")
    store.close()
    with pytest.raises(DomainError) as error:
        Store(path)
    assert error.value.code == "SCHEMA_VERSION_MISMATCH"


def test_stale_evaluation_lease_does_not_lock_the_lab(tmp_path):
    """进程崩在评测中途时，过期租约不能永久锁死工作台。"""
    from datetime import UTC, datetime, timedelta

    from app.repositories.store import EvaluationLease

    store = Store(tmp_path / "lease.db")
    with store.session() as session, session.begin():
        session.add(
            EvaluationLease(
                id=1,
                holder="eval_stale",
                acquired_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            )
        )
    assert store.evaluation_busy() is False
    store.close()
