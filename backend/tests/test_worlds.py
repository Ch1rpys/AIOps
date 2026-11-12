"""World 路径隔离。见计划 §5.2。"""

from app.config import COMPOSE_PROJECT, Settings
from app.worlds import (
    PRIMARY_NGINX_ACTIVE,
    TWIN_NGINX_ACTIVE,
    TWIN_PROJECT,
    world_from_settings,
)


def test_primary_world_matches_existing_defaults():
    settings = Settings(_env_file=None)
    world = world_from_settings(settings, "primary")
    assert world.name == "primary"
    assert world.project == COMPOSE_PROJECT == "aiops-demo"
    assert world.mysql_port == 13306
    assert world.redis_port == 16379
    assert world.app_url == "http://127.0.0.1:18000"
    assert world.nginx_url == "http://127.0.0.1:18080"
    assert world.nginx_active == PRIMARY_NGINX_ACTIVE
    assert world.network == "aiops-demo_default"


def test_twin_world_uses_isolated_project_ports_and_paths():
    settings = Settings(_env_file=None)
    world = world_from_settings(settings, "twin")
    assert world.project == TWIN_PROJECT == "aiops-twin"
    assert world.mysql_port == 23306
    assert world.redis_port == 26379
    assert world.app_url == "http://127.0.0.1:28000"
    assert world.nginx_url == "http://127.0.0.1:28080"
    assert world.nginx_active == TWIN_NGINX_ACTIVE
    assert world.nginx_active != PRIMARY_NGINX_ACTIVE
    assert world.network == "aiops-twin_default"


def test_writing_twin_nginx_does_not_change_primary_bytes():
    before = PRIMARY_NGINX_ACTIVE.read_bytes()
    original = TWIN_NGINX_ACTIVE.read_bytes()
    try:
        TWIN_NGINX_ACTIVE.write_text("# twin isolation probe\n", encoding="utf-8")
        assert PRIMARY_NGINX_ACTIVE.read_bytes() == before
    finally:
        TWIN_NGINX_ACTIVE.write_bytes(original)
