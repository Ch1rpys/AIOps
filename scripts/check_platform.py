"""只读检查本机运行架构和固定镜像，不启动或修复实验。"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("linux/arm64", "linux/amd64")
DOCKER = ["docker", "--context", "default"]
COMPOSE = [
    *DOCKER,
    "compose",
    "--project-name",
    "aiops-demo",
    "--file",
    str(ROOT / "deploy/docker-compose.yml"),
]
SERVICES = ("demo-mysql", "demo-redis", "demo-app", "demo-nginx", "holmes")
COMMAND_TIMEOUT = 30


class CheckError(Exception):
    pass


def architecture(value: str) -> str:
    if not isinstance(value, str):
        return "unknown"
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "x64": "amd64"}.get(
        value.lower(), value.lower()
    )


def observation(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def require(name: str, condition: bool, detail: str) -> dict:
    return observation(name, "PASS" if condition else "FAIL", detail)


def command_json(arguments: list[str], name: str) -> dict | list:
    # CLI 的错误可能带有配置值，只输出检查名称和退出码，不回显原始 stderr。
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
    }
    try:
        result = subprocess.run(
            arguments,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=COMMAND_TIMEOUT,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"{name}: command unavailable or timed out") from error
    if result.returncode != 0:
        raise CheckError(f"{name}: command failed (exit {result.returncode})")
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise CheckError(f"{name}: invalid JSON response") from error
    if not isinstance(value, (dict, list)):
        raise CheckError(f"{name}: expected a JSON object or array")
    return value


def runtime_checks(target: str, node: dict, docker: dict, endpoint: str) -> list[dict]:
    native = f"linux/{architecture(platform.machine())}"
    engine = f"{docker.get('OSType')}/{architecture(docker.get('Architecture', ''))}"
    node_platform = f"{node.get('platform')}/{architecture(node.get('arch', ''))}"
    version = re.fullmatch(r"v?(\d+)\.(\d+)\.\d+(?:[-+].*)?", node.get("version", ""))
    node_supported = version is not None and tuple(map(int, version.group(1, 2))) >= (22, 12)
    override = os.environ.get("DOCKER_DEFAULT_PLATFORM", "")
    checks = [
        require("host", platform.system() == "Linux" and native == target, f"{native}; {target=}"),
        require("python", sys.version_info[:2] == (3, 11), platform.python_version()),
        require(
            "node",
            node_supported and node_platform == target,
            f"{node.get('version')}; {node_platform}",
        ),
        require("docker", engine == target, engine),
        require(
            "docker_context", endpoint.startswith("unix://"), "default must use a local Unix socket"
        ),
        require(
            "platform_override", not override or override == target, override or "native selection"
        ),
    ]
    if docker.get("MemTotal", 0) < 7 * 1024**3:
        checks.append(observation("memory", "WARN", "recommend 8 GiB assigned to the Ubuntu VM"))
    return checks


def dockerfile_base(relative: str, name: str) -> str:
    dockerfile = (ROOT / relative).read_text(encoding="utf-8")
    base = re.findall(r"^FROM\s+(\S+)\s*$", dockerfile, re.MULTILINE)
    if len(base) != 1:
        raise CheckError(f"{name}: expected one native Dockerfile base image")
    return base[0]


def image_references(compose: dict) -> dict[str, str]:
    services = compose.get("services", {})
    if set(services) != set(SERVICES):
        raise CheckError("compose: unexpected service set")
    images = {}
    for name, item in services.items():
        image = item.get("image")
        if image and not str(image).startswith("aiops-demo"):
            images[name] = image
    images["demo-redis"] = dockerfile_base("demo/images/redis/Dockerfile", "demo-redis")
    images["demo-nginx"] = dockerfile_base("demo/images/nginx/Dockerfile", "demo-nginx")
    images["demo-app-base"] = dockerfile_base("demo/demo-app/Dockerfile", "demo-app")
    return images


def manifest_platforms(manifest: dict) -> dict[str, str]:
    if not isinstance(manifest, dict):
        raise CheckError("image: expected a manifest object")
    entries = manifest.get("manifests")
    if not isinstance(entries, list):
        raise CheckError("image: expected a multi-platform manifest index")
    # unknown/unknown 是构建证明，不能被计为可运行的架构。
    return {
        f"{item['platform']['os']}/{item['platform']['architecture']}": item["digest"]
        for item in entries
        if isinstance(item, dict)
        and isinstance(item.get("platform"), dict)
        and item["platform"].get("os") == "linux"
        and item["platform"].get("architecture") in {"arm64", "amd64"}
        and isinstance(item.get("digest"), str)
    }


def check_images(compose: dict, target: str) -> list[dict]:
    checks = []
    for name, image in image_references(compose).items():
        try:
            manifest = command_json(
                [*DOCKER, "buildx", "imagetools", "inspect", "--raw", image], name
            )
            platforms = manifest_platforms(manifest)
            result = require(name, target in platforms, image)
            result["platform_digests"] = platforms
        except CheckError as error:
            result = observation(name, "FAIL", str(error))
        checks.append(result)
    return checks


def check_containers(target: str) -> list[dict]:
    checks = []
    for name in SERVICES:
        # 固定项目和服务名定位容器；停止的实验容器也要检查，但不能擅自启动。
        rows = command_json(
            [*COMPOSE, "--env-file", str(ROOT / ".env"), "ps", "--all", "--format", "json", name],
            name,
        )
        rows = [rows] if isinstance(rows, dict) else rows
        if len(rows) != 1 or rows[0].get("Project") != "aiops-demo":
            checks.append(observation(name, "FAIL", "expected one container in aiops-demo"))
            continue
        container = command_json([*DOCKER, "container", "inspect", rows[0]["ID"]], name)[0]
        image = command_json([*DOCKER, "image", "inspect", container["Image"]], name)[0]
        actual = f"{image.get('Os')}/{architecture(image.get('Architecture', ''))}"
        checks.append(require(name, actual == target, f"container image {actual}"))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-platform", choices=PLATFORMS)
    parser.add_argument(
        "--images", action="store_true", help="also query registry manifests (network)"
    )
    parser.add_argument(
        "--images-only", action="store_true", help="check manifests without runtime acceptance"
    )
    parser.add_argument(
        "--containers", action="store_true", help="verify existing demo/Holmes image architectures"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.images_only and (not args.target_platform or args.containers):
        parser.error("--images-only requires --target-platform and cannot use --containers")
    target = args.target_platform or f"linux/{architecture(platform.machine())}"
    checks = []
    try:
        if target not in PLATFORMS:
            raise CheckError("only linux/arm64 and linux/amd64 are supported")
        if not args.images_only:
            if platform.system() != "Linux":
                raise CheckError("run inside Ubuntu; macOS users should use scripts/start_mac.sh")
            node = command_json(
                [
                    "node",
                    "-p",
                    "JSON.stringify({version:process.version,arch:process.arch,platform:process.platform})",
                ],
                "node",
            )
            docker = command_json([*DOCKER, "info", "--format", "{{json .}}"], "docker")
            context = command_json([*DOCKER, "context", "inspect", "default"], "docker_context")[0]
            checks.extend(
                runtime_checks(target, node, docker, context["Endpoints"]["docker"]["Host"])
            )
        if args.images or args.images_only:
            compose = command_json(
                [*COMPOSE, "config", "--no-interpolate", "--format", "json"], "compose"
            )
            checks.extend(check_images(compose, target))
        if args.containers:
            checks.extend(check_containers(target))
    except (CheckError, KeyError, TypeError, IndexError, AttributeError) as error:
        detail = (
            str(error) if isinstance(error, CheckError) else "unexpected CLI response structure"
        )
        checks.append(observation("platform_check", "FAIL", detail))
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "image-manifests" if args.images_only else "runtime-prerequisites",
        "target_platform": target,
        "status": "FAIL" if any(item["status"] == "FAIL" for item in checks) else "PASS",
        "checks": checks,
        "fault_cycle_acceptance": "NOT RUN",
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for item in checks:
        print(f"{item['status']} {item['name']}: {item['detail']}")
    print(f"{report['status']} {report['scope']}: {target}; fault cycle NOT RUN")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
