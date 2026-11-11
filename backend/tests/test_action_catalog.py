import pytest
from app.schemas.domain import Plan, Scenario
from app.services.action_catalog import CATALOG, lookup
from app.services.executor import plan_violations


def test_extra_spaces_still_match_a_catalog_action():
    spaced = lookup("start    demo-mysql")
    assert spaced is not None
    assert spaced.action_id == "START_DEMO_MYSQL"
    command = lookup("docker   compose    restart    demo-mysql")
    assert command is not None
    assert command.action_id == "LIFE_DEMO_MYSQL_RESTART"
    timeout = lookup("nginx   timeout   1    2     2")
    assert timeout is not None
    assert timeout.action_id == "NGINX_T1_2_2"
    folded = Plan(
        action_id="START   _  DEMO   _ MYSQL",
        target="demo   -   mysql",
        reason="TEST/MOCK",
        risk="LOW",
        expected_checks=["mysql _ select _ 1", "app-db-check"],
    )
    assert folded.action_id == "START_DEMO_MYSQL"
    assert folded.target == "demo-mysql"
    assert folded.expected_checks == ["mysql_select_1", "app_db_check"]
    assert lookup("rm    -rf    /") is None
    assert lookup('start "demo-mysql"') is not None
    assert lookup("SELECT 1;").action_id == "MYSQL_SELECT_1"
    assert lookup("nginx timeout 1s 2s 2s").action_id == "NGINX_T1_2_2"
    assert lookup("重启 MySQL").action_id == "LIFE_DEMO_MYSQL_RESTART"
    assert lookup("restart aiops-demo-demo-mysql-1").action_id == "LIFE_DEMO_MYSQL_RESTART"
    assert lookup("demo-\u200bmysql restart").action_id == "LIFE_DEMO_MYSQL_RESTART"
    assert lookup("不要 restart demo-mysql") is None
    assert lookup("restart demo-mysql demo-redis") is None
    messy = Plan.model_validate(
        {
            "action_id": "重启   MySQL",
            "target": "localhost",
            "reason": "TEST/MOCK",
            "risk": "低",
            "expected_checks": "mysql_select_1, app_db_check",
            "command": "rm -rf /",
        }
    )
    assert messy.action_id == "LIFE_DEMO_MYSQL_RESTART"
    assert messy.target == "demo-mysql"
    assert messy.risk == "LOW"
    assert messy.expected_checks == ["mysql_select_1", "app_db_check"]
    assert len(CATALOG) >= 1000
    assert len({entry.action_id for entry in CATALOG.values()}) == len(CATALOG)
    assert lookup("DELETE_ALL") is None
    assert lookup("START_DEMO_MYSQL") is not None
    assert lookup("NGINX_T1_2_2") is not None


@pytest.mark.parametrize(
    "raw,action_id",
    [
        ("docker compose up -d demo-mysql", "LIFE_DEMO_MYSQL_START"),
        ("docker compose up --force-recreate demo-mysql", "LIFE_DEMO_MYSQL_RECREATE"),
        ("可以使用 START_DEMO_MYSQL", "START_DEMO_MYSQL"),
        ("动作：START_DEMO_MYSQL", "START_DEMO_MYSQL"),
        ("restart: demo-mysql", "LIFE_DEMO_MYSQL_RESTART"),
        ("请先把 MySQL 拉起来", "LIFE_DEMO_MYSQL_START"),
        ("把mysql起起来", "LIFE_DEMO_MYSQL_START"),
        ("恢复MySQL", "LIFE_DEMO_MYSQL_START"),
        ("开机 mysql", "LIFE_DEMO_MYSQL_START"),
        ("拉起 redis", "LIFE_DEMO_REDIS_START"),
        ("杀掉 mysql", "LIFE_DEMO_MYSQL_KILL"),
        ("重新创建 mysql", "LIFE_DEMO_MYSQL_RECREATE"),
        ("restart db", "LIFE_DEMO_MYSQL_RESTART"),
        ("restart cache", "LIFE_DEMO_REDIS_RESTART"),
        ("restart proxy", "LIFE_DEMO_NGINX_RESTART"),
        ("restart the database", "LIFE_DEMO_MYSQL_RESTART"),
        ("reboot mysql", "LIFE_DEMO_MYSQL_RESTART"),
        ("apply nginx timeout 1 2 2", "NGINX_T1_2_2"),
        ("nginx timeout 1/2/2", "NGINX_T1_2_2"),
        ("NGINX_T1/2/2", "NGINX_T1_2_2"),
        ("将nginx超时改为1秒、2秒、2秒", "NGINX_T1_2_2"),
        ("nginx timeout 1.0 2.0 2.0", "NGINX_T1_2_2"),
        ("nginx timeout 1000ms 2000ms 2000ms", "NGINX_T1_2_2"),
        (
            "proxy_connect_timeout 1s proxy_read_timeout 2s proxy_send_timeout 2s",
            "NGINX_T1_2_2",
        ),
        ("restore nginx", "RESTORE_NGINX_UPSTREAM"),
        ("恢复 nginx 配置", "RESTORE_NGINX_UPSTREAM"),
        ("reload nginx", "RESTORE_NGINX_UPSTREAM"),
        ("把应用改回正常", "RESTORE_APP_MODE"),
        ("fix app 500", "RESTORE_APP_MODE"),
        ("关闭 500", "RESTORE_APP_MODE"),
        ("set app mode ok", "RESTORE_APP_MODE"),
        ("什么都不做", "NO_ACTION"),
        ("do nothing", "NO_ACTION"),
        ("mysql -e SELECT 1", "MYSQL_SELECT_1"),
        ("在 MySQL 执行 SELECT 1", "MYSQL_SELECT_1"),
        ("SELECT 1 FROM DUAL", "MYSQL_SELECT_1"),
        ("show full processlist", "MYSQL_SHOW_PROCESSLIST"),
        ("redis-cli ping", "REDIS_PING"),
        ("请 ping redis", "REDIS_PING"),
        ("GET health", "REDIS_GET_HEALTH"),
        ("SET health 1", "REDIS_SET_HEALTH"),
        ("disconnect demo-mysql", "NET_DISCONNECT_DEMO_MYSQL"),
        ("reset mysql password", "MYSQL_RESET_DEMO_PASSWORD"),
        ("恢复密码", "MYSQL_RESET_DEMO_PASSWORD"),
        ("restart demo-mysql && mysql_select_1", "LIFE_DEMO_MYSQL_RESTART__THEN__MYSQL_SELECT_1"),
        ("重启 MySQL 然后检查 mysql_select_1", "LIFE_DEMO_MYSQL_RESTART__THEN__MYSQL_SELECT_1"),
        ("START_DEMO_MYSQL then check mysql_select_1", "START_DEMO_MYSQL__THEN__MYSQL_SELECT_1"),
        ("ＳＴＡＲＴ＿ＤＥＭＯ＿ＭＹＳＱＬ", "START_DEMO_MYSQL"),
        ("action_id=START_DEMO_MYSQL", "START_DEMO_MYSQL"),
    ],
)
def test_messy_llm_commands_map_to_catalog(raw, action_id):
    entry = lookup(raw)
    assert entry is not None
    assert entry.action_id == action_id


@pytest.mark.parametrize(
    "raw",
    [
        "apply restart",
        "rm -rf /",
        "FLUSHALL",
        "docker compose down",
        "can't restart mysql",
        "not restart demo-mysql",
        "不应该重启 mysql",
        "不要停止 mysql 而是重启 mysql",
        "restart mysql && restart redis",
        "先重启 mysql 再重启 redis",
        "ping",
        "info",
    ],
)
def test_ambiguous_or_hostile_commands_stay_rejected(raw):
    assert lookup(raw) is None


def test_plan_accepts_field_aliases_and_keeps_registered_checks():
    aliased = Plan.model_validate(
        {
            "command": "docker compose up -d demo-mysql",
            "target": "localhost",
            "rationale": "TEST/MOCK",
            "risk": "低危",
            "checks": "mysql select 1 and app_db_check",
        }
    )
    assert aliased.action_id == "LIFE_DEMO_MYSQL_START"
    assert aliased.target == "demo-mysql"
    assert aliased.risk == "LOW"
    assert aliased.expected_checks == ["mysql_select_1", "app_db_check"]
    chinese = Plan.model_validate(
        {
            "动作": "START_DEMO_MYSQL",
            "目标": "demo-mysql",
            "原因": "TEST/MOCK",
            "风险": "低",
            "探针": ["SELECT 1"],
        }
    )
    assert chinese.action_id == "START_DEMO_MYSQL"
    assert chinese.expected_checks == ["mysql_select_1"]
    both = Plan.model_validate(
        {
            "action_id": "START_DEMO_MYSQL",
            "command": "rm -rf /",
            "target": "demo-mysql",
            "reason": "TEST/MOCK",
            "risk": "Risk.LOW",
            "expected_checks": ["mysql_select_1", "and", "app_db_check"],
        }
    )
    assert both.action_id == "START_DEMO_MYSQL"
    assert both.expected_checks == ["mysql_select_1", "app_db_check"]
    schema = Plan.model_json_schema()
    assert "action_id" in schema["properties"]
    assert "command" not in schema.get("required", [])


def test_alternate_catalog_route_is_not_illegal():
    plan = Plan(
        action_id="LIFE_DEMO_MYSQL_RESTART__THEN__MYSQL_SELECT_1",
        target="demo-mysql",
        reason="TEST/MOCK",
        risk="LOW",
        expected_checks=["mysql_select_1"],
    )
    assert plan_violations(plan, Scenario.REDIS_DOWN) == []
