import time
from collections.abc import Callable

import httpx
import pymysql
import redis

from app.config import Settings
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
        return [
            self.sql(),
            self.http(
                "app_db_check", self.settings.app_url + "/db-check", {"status": "ok", "value": 1}
            ),
        ]

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
            return [
                self.redis_roundtrip(),
                self.http(
                    "app_cache_check",
                    self.settings.app_url + "/cache-check",
                    {"status": "ok", "value": "probe"},
                ),
            ]
        return [
            self.http(
                "nginx_hello",
                self.settings.nginx_url + "/api/hello",
                {"message": "hello from demo-app"},
            )
        ]

    def baseline(self) -> list[Probe]:
        return [
            *self.mysql(),
            *self.for_scenario(Scenario.REDIS_DOWN),
            *self.for_scenario(Scenario.NGINX_502),
            self.http("app_health", self.settings.app_url + "/health", {"status": "ok"}),
        ]

    def verify(
        self, on_sample: Callable[[list[Probe]], None], scenario: Scenario = Scenario.MYSQL_DOWN
    ) -> Verification:
        samples = []
        for index in range(self.settings.verification_attempts):
            if index:
                time.sleep(self.settings.verification_interval)
            sample = self.for_scenario(scenario)
            samples.append(sample)
            on_sample(sample)
            if judge(sample, scenario) == Verdict.PASS:
                break
        return Verification(verdict=judge(samples[-1], scenario), samples=samples)
