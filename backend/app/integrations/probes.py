import math
import time
from collections.abc import Callable

import httpx
import pymysql
import redis

from app.config import CHECK_IDS, Settings
from app.scenarios import SCENARIOS
from app.schemas.domain import Probe, Scenario, Verdict, Verification


def judge(probes: list[Probe], scenario: Scenario = Scenario.MYSQL_DOWN) -> Verdict:
    by_name = {probe.name: probe.verdict for probe in probes}
    # 必需探针缺失或执行器无有效观测时，不能声称业务恢复。
    if (
        any(name not in by_name for name in SCENARIOS[scenario].checks)
        or Verdict.UNKNOWN in by_name.values()
    ):
        return Verdict.UNKNOWN
    return Verdict.PASS if all(v == Verdict.PASS for v in by_name.values()) else Verdict.FAIL


class Probes:
    def __init__(self, settings: Settings, client: httpx.Client):
        self.settings = settings
        self.client = client

    def sql(self) -> Probe:
        if not self.settings.demo_mysql_password.get_secret_value():
            return Probe(
                name="mysql_select_1",
                verdict=Verdict.UNKNOWN,
                observed={"error": "MYSQL_CREDENTIALS_MISSING"},
            )
        try:
            with pymysql.connect(
                host=self.settings.mysql_host,
                port=self.settings.mysql_port,
                user=self.settings.mysql_user,
                password=self.settings.demo_mysql_password.get_secret_value(),
                database="demo",
                connect_timeout=self.settings.probe_timeout,
                read_timeout=self.settings.probe_timeout,
                write_timeout=self.settings.probe_timeout,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    value = cursor.fetchone()
                    return Probe(
                        name="mysql_select_1",
                        verdict=Verdict.PASS if value == (1,) else Verdict.FAIL,
                        observed={"query": "SELECT 1", "row": list(value or [])},
                    )
        except pymysql.MySQLError as error:
            code = error.args[0] if error.args else "MYSQL_ERROR"
            # 连接拒绝是故障证据；认证失败和探针配置错误属于未知观测。
            verdict = Verdict.UNKNOWN if code in {1044, 1045, 1049} else Verdict.FAIL
            return Probe(name="mysql_select_1", verdict=verdict, observed={"mysql_error": code})
        except (OSError, ValueError, RuntimeError) as error:
            return Probe(
                name="mysql_select_1",
                verdict=Verdict.UNKNOWN,
                observed={"error": type(error).__name__},
            )

    def http(self, name: str, url: str, expected: dict) -> Probe:
        try:
            response = self.client.get(url, timeout=self.settings.probe_timeout)
            try:
                body = response.json()
            except ValueError:
                body = None
            verdict = (
                Verdict.PASS if response.status_code == 200 and body == expected else Verdict.FAIL
            )
            return Probe(
                name=name,
                verdict=verdict,
                observed={"status_code": response.status_code, "body": body},
            )
        except httpx.HTTPError as error:
            return Probe(
                name=name, verdict=Verdict.UNKNOWN, observed={"error": type(error).__name__}
            )

    def mysql(self) -> list[Probe]:
        return [self.by_name("mysql_select_1"), self.by_name("app_db_check")]

    def by_name(self, name: str) -> Probe:
        """按注册的检查 ID 取一次观测。

        注册表是唯一的检查入口：模型只能从这里选要补什么，不能自由发挥。
        """
        if name == "mysql_select_1":
            return self.sql()
        if name == "app_db_check":
            return self.http(
                "app_db_check", self.settings.app_url + "/db-check", {"status": "ok", "value": 1}
            )
        if name == "redis_roundtrip":
            return self.redis_roundtrip()
        if name == "app_cache_check":
            return self.http(
                "app_cache_check",
                self.settings.app_url + "/cache-check",
                {"status": "ok", "value": "probe"},
            )
        if name == "nginx_hello":
            return self.http(
                "nginx_hello",
                self.settings.nginx_url + "/api/hello",
                {"message": "hello from demo-app"},
            )
        if name == "app_health":
            return self.http("app_health", self.settings.app_url + "/health", {"status": "ok"})
        raise ValueError(f"未注册的检查 ID：{name}")

    def redis_roundtrip(self) -> Probe:
        try:
            with redis.Redis(
                host="127.0.0.1",
                port=self.settings.redis_port,
                socket_connect_timeout=self.settings.probe_timeout,
                socket_timeout=self.settings.probe_timeout,
            ) as connection:
                ping = connection.ping()
                connection.set("aiops:health", "probe", ex=30)
                value = connection.get("aiops:health")
                return Probe(
                    name="redis_roundtrip",
                    verdict=Verdict.PASS if ping and value == b"probe" else Verdict.FAIL,
                    observed={"ping": ping, "value": value.decode() if value else None},
                )
        except redis.RedisError as error:
            verdict = Verdict.FAIL if isinstance(error, redis.ConnectionError) else Verdict.UNKNOWN
            if isinstance(error, (redis.AuthenticationError, redis.TimeoutError)):
                verdict = Verdict.UNKNOWN
            return Probe(
                name="redis_roundtrip", verdict=verdict, observed={"error": type(error).__name__}
            )

    def for_scenario(self, scenario: Scenario) -> list[Probe]:
        if scenario == Scenario.MYSQL_DOWN:
            return self.mysql()
        if scenario == Scenario.REDIS_DOWN:
            return [self.by_name("redis_roundtrip"), self.by_name("app_cache_check")]
        return [self.by_name("nginx_hello")]

    def baseline(self) -> list[Probe]:
        return [self.by_name(name) for name in CHECK_IDS]

    def verify(
        self, on_sample: Callable[[list[Probe]], None], scenario: Scenario = Scenario.MYSQL_DOWN
    ) -> Verification:
        """复测采样。

        默认关闭窗口时行为与现状完全一致：一次完整采样 PASS 即结束。
        开启窗口时按「连续健康次数 + 观察窗口」判定，并在 window 里记录
        失败次数与「恢复不稳定」标记；失败采样不会被删除。
        """
        window_enabled = self.settings.verification_window_enabled
        interval = max(self.settings.verification_interval, 0.1)
        if window_enabled:
            # 观察窗口必须真的能被采满：样本数下限之外，还要够覆盖窗口时长，
            # 否则「窗口秒数」只是一个永远达不到的条件（死参数）。
            needed = (
                math.ceil(self.settings.verification_window_seconds / interval)
                + self.settings.verification_required_passes
            )
            limit = max(self.settings.verification_window_samples, needed)
        else:
            limit = self.settings.verification_attempts
        samples: list[list[Probe]] = []
        failures = 0
        consecutive = 0
        started = time.monotonic()
        for index in range(limit):
            if index:
                time.sleep(self.settings.verification_interval)
            sample = self.for_scenario(scenario)
            samples.append(sample)
            on_sample(sample)
            verdict = judge(sample, scenario)
            if verdict == Verdict.FAIL:
                failures += 1
                consecutive = 0
            elif verdict == Verdict.PASS:
                consecutive += 1
            else:
                # 暂时未知不是失败，也不能当作连续健康。
                consecutive = 0
            if not window_enabled:
                if verdict == Verdict.PASS:
                    break
                continue
            # 窗口模式不在首次失败时停止：只有看到「失败之后又恢复」才算得出「恢复不稳定」。
            if (
                consecutive >= self.settings.verification_required_passes
                and time.monotonic() - started >= self.settings.verification_window_seconds
            ):
                break
        window = None
        if window_enabled:
            window = {
                "enabled": True,
                "required_passes": self.settings.verification_required_passes,
                "window_seconds": self.settings.verification_window_seconds,
                "samples": len(samples),
                "failures": failures,
                "consecutive_passes": consecutive,
                # 窗口内再次失败：恢复不稳定。verdict 语义不变，由这个标记表达。
                "instability": failures > 0,
                "stable": failures == 0
                and consecutive >= self.settings.verification_required_passes,
            }
        return Verification(verdict=judge(samples[-1], scenario), samples=samples, window=window)
