"""本机双击演示：补齐依赖、启动容器和页面，不连接虚拟机。"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
DATA = ROOT / "data"
ENV_FILE = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
NODE_HOME_FILE = TOOLS / "node-home.txt"
DEMOS = ("demo-mysql", "demo-redis", "demo-app", "demo-nginx")
LOCAL_SECRETS = (
    "ADMIN_TOKEN",
    "DEMO_MYSQL_ROOT_PASSWORD",
    "DEMO_MYSQL_PASSWORD",
    "HOLMES_API_KEY",
)
MODEL_KEYS = ("OPENAI_API_KEY", "PLANNER_API_KEY", "VISION_API_KEY")
DOCKER_ENV_BLOCK = {
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_DEFAULT_PLATFORM",
}
UI = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"


class DemoError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def docker_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() not in DOCKER_ENV_BLOCK}


def compose_base() -> list[str]:
    return [
        "docker",
        "--context",
        "default",
        "compose",
        "--project-name",
        "aiops-demo",
        "--env-file",
        str(ENV_FILE),
        "--file",
        str(ROOT / "deploy/docker-compose.yml"),
    ]


def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=ROOT, env=docker_env(), check=False, **kwargs)


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def fill_env(text: str) -> tuple[str, list[str]]:
    """只补空着的本机口令。已有的模型和密钥原样保留。"""
    generated = {key: secrets.token_urlsafe(32) for key in LOCAL_SECRETS}
    seen: set[str] = set()
    filled: list[str] = []
    output: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if raw and not raw.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            value = line.split("=", 1)[1].strip()
            seen.add(key)
            if key in generated and not value:
                output.append(f"{key}={generated[key]}")
                filled.append(key)
                continue
        output.append(line)
    for key, value in generated.items():
        if key not in seen:
            if output and output[-1] != "":
                output.append("")
            output.append(f"{key}={value}")
            filled.append(key)
    body = "\n".join(output)
    if text.endswith("\n") or not text:
        body += "\n"
    return body, filled


def ensure_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        if not EXAMPLE.exists():
            raise DemoError("缺少 .env.example，无法生成本机配置")
        ENV_FILE.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        log("已从 .env.example 生成本机 .env")
    updated, filled = fill_env(ENV_FILE.read_text(encoding="utf-8"))
    if filled:
        temporary = ENV_FILE.with_suffix(".env.tmp")
        temporary.write_text(updated, encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temporary.replace(ENV_FILE)
        log("已补全空的本机口令：" + "、".join(filled))
    elif os.name != "nt":
        ENV_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return parse_env(ENV_FILE.read_text(encoding="utf-8"))


def missing_model_keys(values: dict[str, str]) -> list[str]:
    return [key for key in MODEL_KEYS if not values.get(key)]


def platform_machine() -> str:
    hinted = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    if hinted:
        return hinted
    return platform.machine().lower()


def choose_node_archive(listing: str, system: str, machine: str) -> tuple[str, str]:
    """从 SHASUMS 文本里选出当前系统的 Node 22 包。返回 (版本, 文件名)。"""
    suffix = ".zip" if system == "win" else ".tar.gz"
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    for line in listing.splitlines():
        parts = line.split()
        if not parts:
            continue
        filename = parts[-1]
        if not filename.startswith("node-v") or not filename.endswith(suffix):
            continue
        if system not in filename or arch not in filename:
            continue
        return filename.split("-")[1], filename
    raise DemoError(f"找不到适用于 {system}/{arch} 的 Node 22 官方包")


def node_spec() -> tuple[str, str, str]:
    """返回 (版本, 压缩包名, 下载地址)。"""
    listing = (
        urllib.request.urlopen("https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt", timeout=60)
        .read()
        .decode()
    )
    system = {"darwin": "darwin", "win32": "win"}.get(sys.platform)
    if system is None:
        raise DemoError("当前系统请使用 Windows 或 macOS 的双击启动文件")
    version, filename = choose_node_archive(listing, system, platform_machine())
    return version, filename, f"https://nodejs.org/dist/latest-v22.x/{filename}"


def download(url: str, destination: Path) -> None:
    log(f"正在下载 {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def ensure_node() -> Path:
    if NODE_HOME_FILE.exists():
        home = Path(NODE_HOME_FILE.read_text(encoding="utf-8").strip())
        binary = home / ("node.exe" if os.name == "nt" else "bin/node")
        if binary.exists():
            return home
    version, filename, url = node_spec()
    archive = TOOLS / filename
    unpack = TOOLS / "node-unpack"
    download(url, archive)
    if unpack.exists():
        shutil.rmtree(unpack)
    unpack.mkdir(parents=True)
    if filename.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpack)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(unpack)
    homes = [path for path in unpack.iterdir() if path.is_dir()]
    if len(homes) != 1:
        raise DemoError("Node 压缩包结构无法识别")
    home = homes[0]
    NODE_HOME_FILE.write_text(str(home), encoding="utf-8")
    log(f"Node {version} 已装入 .tools")
    archive.unlink(missing_ok=True)
    return home


def node_env(home: Path) -> dict[str, str]:
    binary_dir = home if os.name == "nt" else home / "bin"
    environment = docker_env()
    environment["PATH"] = str(binary_dir) + os.pathsep + environment.get("PATH", "")
    return environment


def npm_path(home: Path) -> Path:
    if os.name == "nt":
        return home / "npm.cmd"
    return home / "bin" / "npm"


def ensure_frontend(home: Path) -> None:
    vite = ROOT / "frontend/node_modules/vite/bin/vite.js"
    lock = ROOT / "frontend/package-lock.json"
    stamp = ROOT / "frontend/node_modules/.package-lock.json"
    if vite.exists() and stamp.exists() and lock.stat().st_mtime <= stamp.stat().st_mtime:
        return
    log("正在按锁文件安装前端依赖")
    result = subprocess.run(
        [str(npm_path(home)), "ci", "--no-audit", "--no-fund"],
        cwd=ROOT / "frontend",
        env=node_env(home),
        check=False,
    )
    if result.returncode != 0:
        raise DemoError("前端依赖安装失败")


def docker_cli() -> str | None:
    return shutil.which("docker")


def start_docker_desktop() -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Docker"], check=False)
        return
    program = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidate = Path(program) / "Docker" / "Docker" / "Docker Desktop.exe"
    if candidate.exists():
        subprocess.Popen([str(candidate)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_docker() -> None:
    if docker_cli() is None:
        start_docker_desktop()
    else:
        probe = run(["docker", "--context", "default", "info"], capture_output=True, text=True)
        if probe.returncode != 0:
            start_docker_desktop()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if docker_cli() is None:
            time.sleep(3)
            continue
        probe = run(["docker", "--context", "default", "info"], capture_output=True, text=True)
        if probe.returncode == 0:
            return
        time.sleep(3)
    if docker_cli() is None:
        raise DemoError(
            "没有找到 Docker Desktop。请先安装后重新双击：https://docs.docker.com/desktop/"
        )
    raise DemoError(
        "Docker 已安装，但 docker --context default 仍不可用。"
        "请在 Docker Desktop 设置中允许使用默认套接字后重新双击。"
    )


def demo_containers_exist() -> bool:
    result = run(
        [*compose_base(), "ps", "--all", "--quiet", *DEMOS],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DemoError("无法查询 demo 容器状态")
    return bool(result.stdout.strip())


def start_containers() -> None:
    if demo_containers_exist():
        log("已有 demo 容器，保留当前状态")
    else:
        log("正在创建四个 demo 容器")
        created = run(
            [
                *compose_base(),
                "up",
                "-d",
                "--build",
                "--wait",
                "--wait-timeout",
                "180",
                *DEMOS,
            ]
        )
        if created.returncode != 0:
            raise DemoError("demo 容器启动失败")
    log("正在按当前配置启动 Holmes")
    holmes = run([*compose_base(), "up", "-d", "--wait", "--wait-timeout", "180", "holmes"])
    if holmes.returncode != 0:
        raise DemoError("Holmes 启动失败")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def require_ports_free() -> None:
    busy = [port for port in (8000, 5173) if port_open(port)]
    if busy:
        names = "、".join(str(port) for port in busy)
        raise DemoError(f"端口 {names} 已被占用。请先关掉上一次的演示窗口，再重新双击。")


def open_browser() -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", UI], check=False)
    elif sys.platform == "win32":
        os.startfile(UI)  # noqa: S606 - 只打开本机演示页
    else:
        subprocess.run(["xdg-open", UI], check=False)


def spawn(
    arguments: list[str], log_path: Path, environment: dict[str, str], cwd: Path
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    kwargs: dict = {"cwd": cwd, "env": environment, "stdout": handle, "stderr": subprocess.STDOUT}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return subprocess.Popen(arguments, **kwargs)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
        return
    os.killpg(process.pid, signal.SIGTERM)


def wait_until_ready(processes: list[subprocess.Popen]) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        for process in processes:
            if process.poll() not in {None, 0}:
                raise DemoError("页面或 API 启动失败，请查看 data/backend.log 与 data/frontend.log")
        if port_open(8000) and port_open(5173):
            return
        time.sleep(0.5)
    raise DemoError("页面或 API 在 90 秒内没有就绪，请查看 data 目录里的日志")


def warn_model_keys(values: dict[str, str]) -> None:
    missing = missing_model_keys(values)
    if not missing:
        return
    log("页面可以打开。以下模型密钥仍为空，诊断或识图要等填进 .env 后再启动：")
    for key in missing:
        log(f"  {key}")


def serve(node_home: Path, values: dict[str, str]) -> None:
    require_ports_free()
    environment = node_env(node_home)
    backend = spawn(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            "backend",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--workers",
            "1",
        ],
        DATA / "backend.log",
        environment,
        ROOT,
    )
    frontend = spawn(
        [str(npm_path(node_home)), "run", "dev"],
        DATA / "frontend.log",
        environment,
        ROOT / "frontend",
    )
    try:
        wait_until_ready([backend, frontend])
        open_browser()
        token = values.get("ADMIN_TOKEN", "")
        log(f"演示已打开：{UI}")
        log("在页面右上角「设置」粘贴管理员口令后才能注入故障。")
        if token:
            log(f"ADMIN_TOKEN={token}")
        log("关闭此窗口会停止页面和 API。Docker 里的实验容器会继续保留。")
        while True:
            if backend.poll() not in {None, 0} or frontend.poll() not in {None, 0}:
                raise DemoError("页面或 API 中途退出，请查看 data 目录里的日志")
            time.sleep(1)
    finally:
        stop(backend)
        stop(frontend)


def main() -> int:
    try:
        values = ensure_env()
        node_home = ensure_node()
        ensure_frontend(node_home)
        wait_for_docker()
        start_containers()
        warn_model_keys(values)
        serve(node_home, values)
    except DemoError as error:
        log(f"FAIL {error}")
        return 1
    except KeyboardInterrupt:
        log("已停止页面和 API")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
