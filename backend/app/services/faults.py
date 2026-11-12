"""故障布置与拆除。只调用已注册的控制器方法，不拼命令。"""

from app.config import SERVICES, DomainError
from app.scenarios import SCENARIOS
from app.schemas.domain import Scenario
from app.services.compiler.apply import apply_recipe, undo_recipe
from app.services.compiler.canonical import canonical_recipe


def apply_fault(docker, scenario: Scenario) -> dict:
    definition = SCENARIOS[scenario]
    recipe = canonical_recipe(scenario)
    output = apply_recipe(docker, recipe)
    if definition.inject == "stop_pair" and isinstance(output, dict) and "outputs" in output:
        return {"operation": "stop_pair", "outputs": output["outputs"]}
    return output


def clear_fault(docker, scenario: Scenario) -> dict:
    definition = SCENARIOS[scenario]
    recipe = canonical_recipe(scenario)
    output = undo_recipe(docker, recipe)
    if definition.inject == "stop_pair" and isinstance(output, dict) and "outputs" in output:
        return {"operation": "start_pair", "outputs": output["outputs"]}
    return output


def clear_lab(docker) -> list[dict]:
    """重置实验环境：先收回配置类故障，再启动四个服务。两界各自调用。"""
    outputs = [
        docker.apply_nginx_upstream("healthy"),
        docker.apply_app_mode("ok"),
    ]
    if hasattr(docker, "reset_demo_password"):
        try:
            outputs.append(docker.reset_demo_password())
        except Exception:
            pass
    for service in SERVICES:
        if hasattr(docker, "clear_netem"):
            try:
                outputs.append(docker.clear_netem(service))
            except Exception:
                pass
        if hasattr(docker, "update_resources"):
            try:
                outputs.append(docker.update_resources(service, cpus="0", memory="0"))
            except Exception:
                pass
        if hasattr(docker, "network"):
            try:
                outputs.append(docker.network(service, "connect"))
            except Exception:
                pass
        outputs.append(docker.change(service, "start"))
    if hasattr(docker, "set_mysql_max_connections"):
        try:
            from app.integrations.docker import DEFAULT_MYSQL_MAX_CONNECTIONS

            outputs.append(docker.set_mysql_max_connections(DEFAULT_MYSQL_MAX_CONNECTIONS))
        except Exception:
            pass
    if hasattr(docker, "set_redis_config"):
        try:
            outputs.append(docker.set_redis_config("maxmemory", "0"))
        except Exception:
            pass
    return outputs
