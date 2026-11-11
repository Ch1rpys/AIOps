"""故障布置与拆除。只调用已注册的控制器方法，不拼命令。"""

from app.config import SERVICES, DomainError
from app.scenarios import SCENARIOS
from app.schemas.domain import Scenario


def apply_fault(docker, scenario: Scenario) -> dict:
    definition = SCENARIOS[scenario]
    if definition.inject == "stop":
        return docker.change(definition.target, "stop")
    if definition.inject == "nginx_upstream":
        return docker.apply_nginx_upstream("misdirected")
    if definition.inject == "app_http_500":
        return docker.apply_app_mode("http_500")
    raise DomainError("BLOCKED", "未注册的注入方式")


def clear_fault(docker, scenario: Scenario) -> dict:
    definition = SCENARIOS[scenario]
    if definition.inject == "stop":
        return docker.change(definition.target, "start")
    if definition.inject == "nginx_upstream":
        return docker.apply_nginx_upstream("healthy")
    if definition.inject == "app_http_500":
        return docker.apply_app_mode("ok")
    raise DomainError("BLOCKED", "未注册的恢复方式")


def clear_lab(docker) -> list[dict]:
    """重置实验环境：先收回配置类故障，再启动四个服务。"""
    outputs = [
        docker.apply_nginx_upstream("healthy"),
        docker.apply_app_mode("ok"),
    ]
    outputs.extend(docker.change(service, "start") for service in SERVICES)
    return outputs
