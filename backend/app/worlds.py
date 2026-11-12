"""两个物理世界：真环境 primary 与影子 twin。

见 docs/影子世界与故障编译器计划.md §5.2。
DockerController / Probes 只对某个 World 说话，禁止再读模块级项目名写文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.config import COMPOSE_PROJECT, ROOT

if TYPE_CHECKING:
    from app.config import Settings

TWIN_PROJECT = "aiops-twin"
PRIMARY_COMPOSE = ROOT / "deploy/docker-compose.yml"
TWIN_COMPOSE = ROOT / "deploy/docker-compose.twin.yml"
PRIMARY_NGINX_ACTIVE = ROOT / "demo/nginx/active/default.conf"
TWIN_NGINX_ACTIVE = ROOT / "demo/twin/nginx/active/default.conf"
PRIMARY_APP_MODE = ROOT / "demo/app-control/mode"
TWIN_APP_MODE = ROOT / "demo/twin/app-control/mode"


@dataclass(frozen=True)
class World:
    name: Literal["primary", "twin"]
    project: str
    compose_file: Path
    mysql_port: int
    redis_port: int
    app_url: str
    nginx_url: str
    nginx_active: Path
    app_mode_file: Path
    network: str


def world_from_settings(settings: Settings, name: Literal["primary", "twin"] = "primary") -> World:
    """按当前 Settings 构造 World。primary 端口与现有默认值逐字一致。"""
    if name == "twin":
        return World(
            name="twin",
            project=TWIN_PROJECT,
            compose_file=TWIN_COMPOSE,
            mysql_port=settings.twin_mysql_port,
            redis_port=settings.twin_redis_port,
            app_url=settings.twin_app_url,
            nginx_url=settings.twin_nginx_url,
            nginx_active=TWIN_NGINX_ACTIVE,
            app_mode_file=TWIN_APP_MODE,
            network=f"{TWIN_PROJECT}_default",
        )
    return World(
        name="primary",
        project=COMPOSE_PROJECT,
        compose_file=PRIMARY_COMPOSE,
        mysql_port=settings.mysql_port,
        redis_port=settings.redis_port,
        app_url=settings.app_url,
        nginx_url=settings.nginx_url,
        nginx_active=PRIMARY_NGINX_ACTIVE,
        app_mode_file=PRIMARY_APP_MODE,
        network=f"{COMPOSE_PROJECT}_default",
    )
