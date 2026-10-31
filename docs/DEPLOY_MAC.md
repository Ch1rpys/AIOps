# Mac 部署：Docker Desktop 与 Ubuntu ARM64

本文对应 F18。组员可按以下步骤尝试 Docker Desktop 本机开发，也可沿用 Ubuntu 虚拟机方式。现有比赛环境适配按 **Mac M5、16GB 统一内存 + Ubuntu 24.04 LTS ARM64**规划；Windows 开发和 Ubuntu amd64 部署继续保留。两种 Mac 方式均未完成实机验收。

## Docker Desktop 本机开发（待实机验收）

在 Mac 安装并启动 [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/)，安装 Python 3.11、uv、Node 22.12+ 和 npm。源码在 macOS 上运行前后端，HolmesGPT 和四个 demo 服务在本机 Docker 中运行；无需复制 Windows 开发机的虚拟机或 SSH 配置。

当前控制器固定使用 `docker --context default`。Docker Desktop 启动时通常使用 `desktop-linux`，需在设置中启用 **Allow the default Docker socket to be used**，然后确认 `default` 能连接本机引擎。具体设置见 [Docker 官方说明](https://docs.docker.com/desktop/setup/install/mac-permission-requirements/) 与 [设置说明](https://docs.docker.com/desktop/settings-and-maintenance/settings/)。不修改控制器去连接远程 Docker。

在项目根目录执行首次配置：

```bash
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_DEFAULT_PLATFORM
docker --context default info
cp .env.example .env
chmod 600 .env
# 在编辑器中填写自己的管理员令牌、实验数据库密码和模型配置。
uv python install 3.11
uv sync --frozen
```

首次创建实验服务与 HolmesGPT（已有待审批故障时不要用此命令恢复 demo 服务）：

```bash
docker --context default compose --project-name aiops-demo --env-file .env --file deploy/docker-compose.yml up -d --build --wait --wait-timeout 180 demo-mysql demo-redis demo-app demo-nginx holmes
```

随后在两个终端的项目根目录分别执行：

```bash
bash scripts/start_backend.sh
```

```bash
bash scripts/start_frontend.sh
```

访问 `http://127.0.0.1:5173`，后端为 `http://127.0.0.1:8000`。在项目根目录执行 `bash scripts/run_acceptance.sh --preflight` 做只读预检，再按 README 完成人工审批验收。每位成员的 `.env`、SQLite 工单与依赖均独立创建；首次历史列表为空属于正常状态。

`start_all.sh` 依赖 Linux / `flock`，`check_platform.py` 的运行环境检查也仅支持 Linux，不能用于上述 macOS 本机启动。`start_mac.sh` 专用于下文的 SSH 方案。此处只是基于现有 Compose 和分步脚本整理的部署路径，**macOS 依赖安装、容器启动、模型连通及完整故障闭环均为 NOT RUN**；任一步失败应保留实际输出。

## 1. Ubuntu 虚拟机方式

```text
Mac M5：浏览器 + SSH 本地端口转发
  └─ Ubuntu ARM64 虚拟机（原生虚拟化）
       ├─ Python 3.11：FastAPI / LangGraph / SQLite
       ├─ Node 22.12+：Vue / Vite
       └─ Docker Engine + Compose + Buildx
            ├─ HolmesGPT 0.45.0
            └─ demo-mysql / demo-redis / demo-app / demo-nginx
```

- 建议使用 Ubuntu Server，分配 **4 个 vCPU、8GiB 内存、60GiB 虚拟磁盘**，给 macOS 留出内存；这是起始配置建议，性能与峰值内存仍需 M5 实测。
- UTM 中选择 **Virtualize → Linux → ARM64 ISO**。不要选择 x86 ISO 或将 Windows 上的 amd64 虚拟机磁盘直接作为 ARM64 虚拟机使用。参见 [UTM 官方 Ubuntu 指南](https://docs.getutm.app/guides/ubuntu/)。其他支持 ARM64 Ubuntu 的虚拟机软件也可使用。
- 虚拟机内安装 Docker Engine，不安装 Docker Desktop；容器使用 Ubuntu 内核，不要求在虚拟机内再启动一个虚拟机。Docker 官方支持 [Ubuntu 24.04 ARM64](https://docs.docker.com/engine/install/ubuntu/)。
- 诊断和规划调用远程模型 API，无需本地模型或独立显卡；比赛网络必须能访问模型提供商。镜像、依赖提前下载，也不能替代真实模型联网。
- 源码、SQLite 和 Docker 数据放在 Ubuntu 自己的磁盘中，如 `/home/ubuntu/aiops-platform`，避免在 Mac 共享文件夹中运行数据库和依赖目录。

## 2. 原生架构与依赖

Compose 不写死 `platform: linux/amd64` 或 `linux/arm64`，由本地引擎选择原生镜像。Holmes 使用已锁定的多架构索引 digest；不切换版本、不增加模拟层、不修改其内核。

| 组件 | 当前固定引用 | ARM64 检查范围 |
| --- | --- | --- |
| HolmesGPT | 0.45.0，Compose 中的 sha256 多架构索引 | registry 含 linux/arm64 与 linux/amd64 |
| MySQL | mysql:8.4.7 | 同上 |
| Redis | redis:7.4.7 | 同上 |
| Nginx | nginx:1.28.0-alpine | 同上 |
| demo-app 基础镜像 | python:3.11.14-slim | 同上，应用在目标机原生构建 |

镜像 ARM64 manifest 已在既有 Ubuntu 环境核验；原始本机核验资料不随源码分发。可用下文命令重新检查并输出至自己的 `data/`。镜像可获取相应架构不代表 ARM64 容器运行或故障闭环通过。

已在隔离目录跨平台获取 62 个锁定 Python 包，核对 22 个共享库均为 AArch64；前端通过 `npm ci --ignore-scripts --os=linux --cpu=arm64 --libc=glibc` 获取依赖，esbuild / Rollup 原生文件架构正确。这些检查针对 Linux ARM64 包，程序没有在 ARM64 上执行，不能代替 macOS 依赖安装或目标机运行验证。

在 Ubuntu 中安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Linux ARM64 的 [Node.js](https://nodejs.org/en/download)（22.12+）和 Docker 的 Compose / Buildx 插件。`uv sync --frozen` 按 `uv.lock` 安装 Python 3.11 依赖；`npm ci` 按前端锁文件安装 Linux ARM64 原生依赖。**不要迁移 Windows/amd64 的 `.venv`、`node_modules` 或 Docker 数据卷目录。**

## 3. 首次部署（Ubuntu 里执行）

将完整源码放入 Ubuntu 项目目录。迁移时排除 `.env`、`data/`、`.cache/`、`.venv/`、`node_modules/` 和 SSH 私钥；模型配置单独安全填写。下文假定目录为 `/home/ubuntu/aiops-platform`，请替换为真实账号目录。

```bash
cd /home/ubuntu/aiops-platform
cp .env.example .env
chmod 600 .env
# 在编辑器中填写 .env，不将密钥粘贴到日志或提交到仓库。
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.11
uv sync --frozen
cd frontend
npm ci --no-audit --no-fund
cd ..
```

模型配置和管理员令牌见 [README](../README.md)。Docker 必须是 Ubuntu 的本地 `default` context，当前用户应能运行 `docker --context default info`。

```bash
# 首次联网检查：运行架构 + 固定镜像 ARM64 manifest，失败退出非零。
.venv/bin/python scripts/check_platform.py --target-platform linux/arm64 --images --output data/platform-arm64.json

# 创建/复用服务；已有故障不会因启动入口被自动恢复。
bash scripts/start_all.sh /home/ubuntu/aiops-platform linux/arm64

# 检查实际容器镜像架构，并运行只读基线探针预检。
.venv/bin/python scripts/check_platform.py --target-platform linux/arm64 --containers --output data/platform-arm64-running.json
bash scripts/run_acceptance.sh --preflight
```

启动检查 Python / Node / Docker 与目标架构是否一致，拒绝远程 Docker context 和不一致的 `DOCKER_DEFAULT_PLATFORM`。启动后检查五个实际容器的镜像架构，停止中的故障容器也只读检查，不擅自恢复。常规启动不查询 registry manifest，已准备好的镜像不因这项检查依赖比赛网络。

`WARN memory` 表示虚拟机内存低于建议值；检查 PASS 仅表示对应前置条件满足。探针基线 PASS 也不表示模型调用或完整故障闭环已验收。

## 4. Mac 启动入口

先在 Ubuntu 安装并启用 OpenSSH Server。在 Mac 配置 SSH 密钥登录和主机别名，例如 `~/.ssh/config`：

```sshconfig
Host aiops-ubuntu
    HostName <虚拟机实际地址>
    User ubuntu
    IdentityFile ~/.ssh/aiops_ubuntu_ed25519
    IdentitiesOnly yes
```

SSH 地址可使用虚拟机的共享网络地址；如虚拟机软件使用 SSH 端口映射，则在该配置增加实际 `Port`。首次执行 `ssh aiops-ubuntu true`，核对 Ubuntu 主机指纹后保存，再运行启动入口。脚本严格验证已知主机，不自动接受未知指纹、不接收或保存密码。

Mac 本地保留一份同版本源码，在 Mac 终端执行：

```bash
bash scripts/start_mac.sh aiops-ubuntu /home/ubuntu/aiops-platform
# 只建立访问通道，不自动打开浏览器：
bash scripts/start_mac.sh aiops-ubuntu /home/ubuntu/aiops-platform --no-browser
```

入口自动识别 Apple Silicon（含 Rosetta 终端），要求远端是 ARM64，执行同一 Ubuntu 启动脚本，将 Mac `127.0.0.1:5173 / 8000` 转发到虚拟机。服务和数据库端口继续只绑定 Ubuntu loopback。

终端保持打开；Ctrl+C 只关闭本次 SSH 通道，Ubuntu 服务与工单保留。再次执行可复用远端服务；本机端口被其他会话占用时明确失败，不停止其他进程。该入口不自动同步源码，修改后应完整部署对应源文件与锁文件，再启动。

## 5. 比赛机器验收

在目标 M5 上记录以下实际结果，报告保留在 Ubuntu 的 `data/`，经过脱敏再导出：

1. Ubuntu 冷启动后，架构检查与 `run_acceptance.sh --preflight` PASS，Mac 浏览器可访问实验页、工作台和历史报告。
2. MySQL：真实注入 → 日志/双探针失败 → 真实诊断与规划 → 人工批准 → 恢复 → SELECT 1 与 `/db-check` PASS。
3. Redis：同样完成人工审批，PING / SET / GET 与 `/cache-check` PASS。
4. Nginx：注入时真实 502；恢复后经代理 `/api/hello` HTTP 200 且内容正确。
5. 拒绝审批、刷新/重启恢复、非法动作拦截与 FAIL/UNKNOWN 分支；完整 3×3 评测须另行保存每轮真实记录。

目前没有连接目标 M5，**M5 上的原生安装、容器运行、冷启动与真实闭环均为 NOT RUN**。已有 Ubuntu amd64 报告继续保留，不能作为 M5 验收证据。
