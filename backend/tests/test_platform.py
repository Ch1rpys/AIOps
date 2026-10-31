import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest
import yaml
from app.config import ROOT

spec = importlib.util.spec_from_file_location("platform_check", ROOT / "scripts/check_platform.py")
platform_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform_check)


@pytest.fixture
def arm_runtime(monkeypatch):
    # TEST/MOCK 架构输入，只验证门禁，不代表 ARM64 二进制已运行。
    monkeypatch.setattr(platform_check.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform_check.platform, "machine", lambda: "aarch64")
    monkeypatch.delenv("DOCKER_DEFAULT_PLATFORM", raising=False)
    return (
        {"platform": "linux", "arch": "arm64", "version": "v22.23.4"},
        {"OSType": "linux", "Architecture": "aarch64", "MemTotal": 8 * 1024**3},
    )


def test_arm64_runtime_accepts_native_aliases(arm_runtime):
    checks = platform_check.runtime_checks(
        "linux/arm64", *arm_runtime, "unix:///var/run/docker.sock"
    )
    assert all(item["status"] == "PASS" for item in checks)


@pytest.mark.parametrize(
    "version,arch", [("v20.19.0", "arm64"), ("v22.11.0", "arm64"), ("v24.0.0", "x64")]
)
def test_runtime_rejects_old_or_foreign_node(arm_runtime, version, arch):
    node, docker = arm_runtime
    node.update(version=version, arch=arch)
    checks = platform_check.runtime_checks(
        "linux/arm64", node, docker, "unix:///var/run/docker.sock"
    )
    assert next(item for item in checks if item["name"] == "node")["status"] == "FAIL"


def test_arm64_target_rejects_x86_host_and_remote_engine(arm_runtime, monkeypatch):
    monkeypatch.setattr(platform_check.platform, "machine", lambda: "x86_64")
    checks = platform_check.runtime_checks("linux/arm64", *arm_runtime, "tcp://TEST-MOCK:2375")
    failures = {item["name"] for item in checks if item["status"] == "FAIL"}
    assert {"host", "docker_context"} <= failures


def test_arm64_rejects_amd64_override(arm_runtime, monkeypatch):
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
    checks = platform_check.runtime_checks(
        "linux/arm64", *arm_runtime, "unix:///var/run/docker.sock"
    )
    assert next(item for item in checks if item["name"] == "platform_override")["status"] == "FAIL"


def test_memory_recommendation_is_not_runtime_acceptance(arm_runtime):
    node, docker = arm_runtime
    docker["MemTotal"] = 4 * 1024**3
    checks = platform_check.runtime_checks(
        "linux/arm64", node, docker, "unix:///var/run/docker.sock"
    )
    assert next(item for item in checks if item["name"] == "memory")["status"] == "WARN"


def test_manifest_attestation_is_not_an_arm64_image():
    manifest = {
        "manifests": [
            {
                "platform": {"os": "linux", "architecture": "amd64"},
                "digest": "sha256:TEST-MOCK-amd64",
            },
            {
                "platform": {"os": "unknown", "architecture": "unknown"},
                "digest": "sha256:TEST-MOCK-attestation",
            },
        ]
    }
    assert set(platform_check.manifest_platforms(manifest)) == {"linux/amd64"}


@pytest.mark.parametrize("manifest", [{"config": {}}, [], {"manifests": "TEST/MOCK"}])
def test_manifest_missing_index_fails(manifest):
    with pytest.raises(platform_check.CheckError):
        platform_check.manifest_platforms(manifest)


def test_registry_failure_and_missing_arm64_are_failures(monkeypatch):
    compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))
    calls = []

    def registry(arguments, name):
        calls.append(arguments)
        if name == "holmes":
            raise platform_check.CheckError("TEST/MOCK registry unavailable")
        return {"manifests": []}

    monkeypatch.setattr(platform_check, "command_json", registry)
    checks = platform_check.check_images(compose, "linux/arm64")
    assert len(checks) == 5 and all(item["status"] == "FAIL" for item in checks)
    assert all(
        call[:6] == ["docker", "--context", "default", "buildx", "imagetools", "inspect"]
        for call in calls
    )


def test_cli_failure_is_nonzero_and_does_not_echo_cli_secrets(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["check_platform.py", "--images-only", "--target-platform", "linux/arm64"]
    )
    monkeypatch.setattr(
        platform_check.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "TEST-MOCK-SECRET"),
    )
    assert platform_check.main() == 1
    output = capsys.readouterr().out
    assert "FAIL" in output and "TEST-MOCK-SECRET" not in output


def test_image_report_does_not_claim_fault_acceptance(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_platform.py",
            "--images-only",
            "--target-platform",
            "linux/arm64",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(platform_check, "command_json", lambda *args: {})
    monkeypatch.setattr(
        platform_check,
        "check_images",
        lambda *args: [{"name": "TEST/MOCK", "status": "PASS", "detail": "test"}],
    )
    assert platform_check.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["scope"] == "image-manifests"
    assert report["fault_cycle_acceptance"] == "NOT RUN"


def test_container_check_uses_explicit_env_and_never_recovers_stopped_services(monkeypatch):
    calls = []

    def docker(arguments, name):
        calls.append(arguments)
        if "compose" in arguments:
            assert arguments[arguments.index("--env-file") + 1] == str(ROOT / ".env")
            return [{"ID": "TEST-MOCK-container", "Project": "aiops-demo"}]
        if "container" in arguments:
            return [{"Image": "TEST-MOCK-image", "State": {"Status": "exited"}}]
        return [{"Os": "linux", "Architecture": "arm64"}]

    monkeypatch.setattr(platform_check, "command_json", docker)
    assert all(item["status"] == "PASS" for item in platform_check.check_containers("linux/arm64"))
    assert not any(action in arguments for arguments in calls for action in ("start", "stop", "up"))


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="requires Linux Bash")
@pytest.mark.parametrize("startup_exit,machine", [(0, "arm64"), (0, "x86_64"), (1, "arm64")])
def test_mac_launcher_passes_arm64_and_stops_on_startup_failure(tmp_path, startup_exit, machine):
    # TEST/MOCK macOS/SSH 边界；真实启动脚本仍由 Bash 解释执行。
    for name, body in {
        "uname": f"print('Darwin' if sys.argv[1] == '-s' else '{machine}')",
        "sysctl": "print('1')",
        "curl": "sys.exit(0)",
        "ssh": """
with open(os.environ['AIOPS_TEST_SSH_CALLS'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if '-N' in sys.argv:
    time.sleep(0.5)
else:
    sys.exit(int(os.environ['AIOPS_TEST_STARTUP_EXIT']))
""",
    }.items():
        stub = tmp_path / name
        stub.write_text(
            f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n", encoding="utf-8"
        )
        stub.chmod(0o700)
    calls_file = tmp_path / "calls.jsonl"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/start_mac.sh"),
            "TEST-MOCK-ubuntu",
            "/home/test/aiops",
            "--no-browser",
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "AIOPS_TEST_SSH_CALLS": str(calls_file),
            "AIOPS_TEST_STARTUP_EXIT": str(startup_exit),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert result.returncode == startup_exit
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert calls[0][-1].endswith("'/home/test/aiops' 'linux/arm64'")
    if startup_exit:
        assert len(calls) == 1 and "PASS http" not in result.stdout
    else:
        assert len(calls) == 2 and "PASS http" in result.stdout
        assert "127.0.0.1:5173:127.0.0.1:5173" in calls[1]
