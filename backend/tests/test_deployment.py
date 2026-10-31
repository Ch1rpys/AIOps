import yaml
from app.config import ROOT


def test_compose_is_isolated_and_uses_fixed_versions():
    compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["name"] == "aiops-demo"
    assert set(compose["services"]) == {
        "demo-mysql",
        "demo-redis",
        "demo-app",
        "demo-nginx",
        "holmes",
    }
    for service in compose["services"].values():
        assert "platform" not in service  # 由本地引擎选择原生架构，兼容 ARM64 与 amd64。
        assert not service.get("privileged") and not service.get("network_mode")
        assert all(port.startswith("127.0.0.1:") for port in service.get("ports", []))
        assert all("docker.sock" not in volume for volume in service.get("volumes", []))
        if "image" in service:
            assert ":" in service["image"] and not service["image"].endswith(":latest")
    assert "SELECT 1" in str(compose["services"]["demo-mysql"]["healthcheck"])
    tools = yaml.safe_load((ROOT / "deploy/holmes-config.yaml").read_text(encoding="utf-8"))
    assert all(item["enabled"] is False for item in tools["toolsets"].values())
