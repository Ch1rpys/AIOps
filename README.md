# AIOps 故障诊断与处置验证平台

本机竞赛 MVP。支持 MYSQL_DOWN、REDIS_DOWN、NGINX_502：真实停止隔离服务 → 状态、日志和探针采证 → HolmesGPT 诊断 → 独立 LLM 规划 → 人工审批 → 固定动作恢复 → 独立复测 → SQLite 记录与报告。

**三个场景各一条后端真实闭环 PASS。** 已在 Ubuntu 完成真实注入、HolmesGPT + DeepSeek 诊断规划、用户批准、白名单恢复和独立探针 PASS，三条成功工单均为 REAL / RESOLVED。现已补齐三场景前端接线、环境管理和历史报告；3×3 评测尚未开始。本机原始记录与开发过程资料不随源码分发，新环境需重新运行验收。

| 场景 | 已有 Ubuntu 环境的恢复验证 |
| --- | --- |
| MySQL | SELECT 1 + /db-check PASS |
| Redis | PING + SET/GET + /cache-check PASS |
| Nginx 502 | 经代理 /api/hello HTTP 200 且内容正确 |

三张场景卡片均按服务端 key 调用对应注入接口，确认对话框展示实际目标；没有证据、容器状态或复测数据时不补造成功结果。用户功能范围见 [功能清单](docs/功能清单.md)。

## 仓库协作与本机文件

本项目统一使用 [GitHub 仓库](https://github.com/Ch1rpys/AIOps)，默认分支为 `main`。首次获取代码：

```bash
git clone https://github.com/Ch1rpys/AIOps.git
cd AIOps
```

已有克隆的成员应确认 `git remote -v` 指向上述仓库。在工作区干净时先同步，修改并通过相关检查后再提交：

```bash
git pull --ff-only
# 修改代码并运行相关检查后，核对待提交文件。
git status --short
git add <本次修改的文件路径>
git commit -m "说明本次修改"
git push
```

组员推送后，再执行 `git pull --ff-only` 获取更新。若提示分支已分叉或推送被拒绝，先 `git fetch origin` 检查差异并合并解决冲突，再推送；不要强制覆盖。多人同时开发时建议各自建立功能分支，通过 Pull Request 合并到 `main`。

提交源码、测试、`uv.lock`、`frontend/package-lock.json`、`.env.example`、Compose、启动脚本及部署说明。每位成员从 `.env.example` 创建自己的 `.env` 并填写配置，在自己的系统上重新安装依赖。

`.gitignore` 排除本机凭据、`data/`（SQLite、检查点、SSH 资料、日志和验收输出）、缓存、依赖、构建产物、编辑器/开发工具配置、开发过程文档及虚拟机镜像。这些文件保留在本机；不需要把 Windows 的虚拟机、`.venv` 或 `node_modules` 发给组员。原始记录如需提交验收，应先脱敏，再单独整理为正式交付资料。

首次提交前用 `git status --short` 核对文件清单；可用 `git check-ignore -v <文件路径>` 查询忽略原因。忽略规则仅作用于尚未跟踪的文件，不会移除已提交历史；直接压缩整个工程目录也不会应用这些规则。

## Mac / Apple Silicon 部署

组员使用 Docker Desktop 时，可在 Mac 本机安装 Python 3.11、uv、Node 22.12+，按 [Mac 部署说明](docs/DEPLOY_MAC.md) 的本机开发步骤运行。该路径不需要 Windows 开发机的虚拟机或 SSH 文件，**macOS 实机安装与三场景闭环仍为 NOT RUN**。

现有 `start_mac.sh` 是连接 Ubuntu 的 SSH 入口，`start_all.sh` 与 `check_platform.py` 的运行环境门禁只适用于 Linux。保留的 Mac M5 + Ubuntu 24.04 ARM64 虚拟机方案、架构检查和验收步骤也见上述说明；镜像 ARM64 manifest 核验不代表 Mac 实机验收通过。

## Ubuntu 部署

日常在 Windows **双击仓库根目录的 `start.cmd`** 即可：连接现有 Ubuntu 虚拟机，启动/复用 Holmes、FastAPI、Vue，建立 5173 / 8000 SSH 转发，检查就绪后打开浏览器。无需分别开多个终端；重复点击复用已有隧道。虚拟机需已开机且网络可达，首次部署仍需安装依赖并填写虚拟机 `.env`。

Windows 启动入口保留原开发机的连接默认值，组员使用时需传入自己的虚拟机地址、账号和项目路径。默认 SSH 文件位于本机 `data/vm-access/ubuntu_ed25519` 和 `known_hosts`，不随仓库分发。脚本不包含私钥或密码，不自动接受未知主机指纹：

```powershell
./scripts/start_all.ps1 -VmHost '虚拟机实际地址' -VmUser '实际账号' -RemoteDirectory '/home/实际账号/aiops-platform'
# 只启动，不打开浏览器：
./start.cmd -NoBrowser
```

支持 `-IdentityFile` / `-KnownHostsFile` 指定已有 SSH 文件。Linux 本机也可运行 `bash scripts/start_all.sh`。当前 Windows 入口会同步 frontend 的 src/public/index.html 以及启动/架构检查脚本；后端代码、前端依赖及锁文件的变更仍需单独部署，`.env` 不随源码同步。

Windows 连接前会将私钥复制到 `data/startup/ssh/<当前用户 SID>/identity`，设置所有者为当前用户、关闭权限继承、仅允许当前用户访问并校验 ACL。沙箱与桌面账号使用各自副本，避免 OpenSSH 报 `UNPROTECTED PRIVATE KEY FILE`；源私钥内容和权限不修改，副本属于已忽略的 `data/`，不会提交。

首次没有 demo 容器时创建四个 demo；已有 demo 一律保留当前状态，避免把待审批故障自动恢复。重启虚拟机后若实验基线不健康，请在实验页检查并确认重置。Holmes 健康检查和 Planner 配置检查不代表真实模型诊断通过。启动失败返回非零；远程日志为 `data/backend.log`、`data/frontend.log`，本机隧道日志为 `data/startup/ssh-error.log`。若本机 5173 / 8000 已由其他程序或旧手工隧道占用，脚本提示冲突，由用户关闭旧会话后重试。

在自己的 Ubuntu 项目目录使用 Python 3.11、uv、Node 22.12+，首次通过 `uv sync --frozen` 与 `frontend` 中的 `npm ci` 安装锁定依赖。Linux 启动脚本：

```bash
export PATH="$HOME/.local/bin:$PATH"
bash scripts/start_lab.sh
bash scripts/start_backend.sh
# 另一终端执行：
bash scripts/start_frontend.sh
```

Linux 实验启动脚本默认启动四个 demo；Holmes 使用同一 Compose 文件的 `holmes` 服务单独启动。服务均保持 loopback 绑定，Windows 通过 SSH 转发 5173 端口访问 Vue，Vue 代理同机 FastAPI。私钥、管理员令牌和数据库密码不随代码同步。

SSH 同时转发 Windows 5173 → VM Vue、8000 → VM FastAPI。虚拟机服务在后台运行，重启虚拟机后需重新启动；SSH 会话关闭后需重建转发。恢复期间的每次探针采样均保存在实际工单中。

DeepSeek 配置采用 `HOLMES_MODEL=deepseek/deepseek-flash`、`PLANNER_MODEL=deepseek-flash`、`PLANNER_URL=https://api.deepseek.com/v1`；`DEEPSEEK_API` 和 `PLANNER_API_KEY` 通过受限 .env 注入，两个 JSON_MODE 均设为 `json_object`。此模式遵循 [DeepSeek 官方 JSON Output](https://api-docs.deepseek.com/guides/json_mode/)，提示中携带 Schema，结果仍用原 Pydantic 契约验证，不放宽动作白名单。默认其他提供商仍使用 `json_schema`。

## 本轮可用范围

- `/lab`：三场景参数化注入、目标确认、互斥与基线门禁，按场景保留网络重试幂等键。
- `/environment`：四容器实际状态、平台/Docker/Holmes/Planner 自检、当前实验租约及鉴权确认重置；`?tab=probes` 查看基线探针完整返回。Planner 配置就绪不等于真实模型连通，仍如实显示 UNKNOWN。
- `/incidents/:id`：服务端工单、按轮次保存的节点记录、诊断引用的实际证据、右侧审批、执行回执及全部复测采样。当前环境采样与工单历史分别标记时间。
- `/incidents`：场景/状态服务端筛选与分页、工单详情和真实 Markdown 报告下载；左侧保留最近六条工单与错误反馈。
- 概览与比赛评测仍留待后续切片，不提供未完成的占位入口或假统计。

## 启动

需要 Python **3.11**、uv、Node **22.12+**（本次使用 24.19.0）、npm，以及 Docker Desktop Linux Containers。固定本地 Docker context 为 `default`；不要切到远程引擎。

从仓库根目录执行：

```powershell
Copy-Item .env.example .env
# 在编辑器中填写 .env，再启动服务。不要提交或输出密钥。
uv sync --frozen --cache-dir .cache/uv
./scripts/start_lab.ps1
```

`.env` 必填：

| 配置 | 用途 |
| --- | --- |
| `ADMIN_TOKEN` | 本机管理员写操作令牌；缺失时只读可用 |
| `DEMO_MYSQL_ROOT_PASSWORD` / `DEMO_MYSQL_PASSWORD` | 仅用于隔离实验数据库，禁止复用生产凭据 |
| `OPENAI_API_KEY` | Holmes 容器的模型凭据 |
| `HOLMES_API_KEY` | Holmes HTTP 服务认证凭据，与模型凭据不同 |
| `HOLMES_MODEL` | Holmes 服务实际可用的模型标识 |
| `PLANNER_URL` / `PLANNER_API_KEY` / `PLANNER_MODEL` | 独立规划调用，URL 填兼容服务的 `/v1` 基础地址 |

Holmes 模型和 Planner 可以使用同一提供商，但诊断和规划为独立调用。需支持 `response_format.json_schema`。没有密钥时不需要创建 `.env` 也能启动只读控制台和测试。

若只有 Docker 和实验数据库配置，可先独立启动四个 demo 服务；Holmes 的空配置不会阻止 Compose 查询实验状态。完整真实验收仍需填写全部模型配置并验证实际调用。

```powershell
docker --context default compose --project-name aiops-demo --env-file .env --file deploy/docker-compose.yml up -d --build --wait demo-mysql demo-redis demo-app demo-nginx
```

分别在两个 PowerShell 窗口运行：

```powershell
./scripts/start_backend.ps1
```

```powershell
./scripts/start_frontend.ps1
```

打开 <http://127.0.0.1:5173>。后端文档为 <http://127.0.0.1:8000/docs>。在“系统设置”输入 `ADMIN_TOKEN` 才能操作；令牌只保存在当前页面内存，刷新后需重输，工单与审批状态仍从数据库读取。输入令牌不代表认证通过，所有写操作由服务端重新校验。

默认端口：平台 API 8000、Vue 5173、Holmes 5050、MySQL 13306、Redis 16379、demo-app 18000、Nginx 18080。全部绑定 `127.0.0.1`。SQLite 业务库和 LangGraph 检查点分别保存在 `data/platform.sqlite3`、`data/checkpoints.sqlite3`；不能删除文件来“恢复”实验。

## 操作和真实验收

1. 实验页重新自检。四个 demo 容器健康且全部基线探针 PASS 才能注入。
2. 确认注入 MySQL 故障，服务端真实停止 `demo-mysql`；确认容器退出、SQL 和 `/db-check` 明确失败，才标为 INJECTED。
3. 在工作台开始诊断。Holmes 先经版本和工具权限核验，再调用 `/api/chat`。真实证据与本地 Runbook 作为上下文传入，当前不使用 HTTP Connector。
4. 读取根因、证据和计划，在右侧明确批准或拒绝。批准前不会执行恢复；拒绝后取消且保留故障，需要手工确认重置。
5. `START_DEMO_MYSQL` 只映射到固定项目的固定服务启动。程序执行 SQL `SELECT 1` 与 `/db-check`，同一轮双探针通过才判 PASS。
6. FAIL 最多再诊断一次，第二轮有独立计划并重新人工审批；UNKNOWN 直接结束。已成功的同一动作不会重复执行。重启中断的执行不自动重放，须检查环境后重置。

真实验收脚本先检查环境，然后要求命令行明确确认注入及**逐轮人工批准**：

```powershell
./scripts/run_acceptance.ps1 -Preflight
./scripts/run_acceptance.ps1
```

脚本输出 PASS/FAIL，退出码成功 0、失败非零。原始 HTTP 响应写入 `data/acceptance/*.json`。基线不健康时停止，不操作其他 Docker 服务。重置只经控制台确认和认证接口；停止演示服务可运行 `./scripts/stop_lab.ps1`，不会删除容器、数据卷或镜像。

## 离线检查

```powershell
./.venv/Scripts/ruff.exe check .
./.venv/Scripts/ruff.exe format --check .
./.venv/Scripts/pytest.exe -q
Set-Location frontend
npm ci --cache ../.cache/npm --no-audit --no-fund
npm run lint
npm run typecheck
npm run build
```

`uv.lock` 与 `frontend/package-lock.json` 锁定依赖。后端测试覆盖真实 LangGraph、SQLAlchemy 和磁盘检查点；只有外部 Docker/探针/模型边界使用测试夹具，所有测试工单标记 `TEST/MOCK`。

浏览器交互测试使用专门入口 `backend/tests/ui_server.py`，数据库为 `data/test-ui`，不能当正式服务部署。正式入口始终是 `app.main:app`，不提供运行时切换到 Mock 的接口或配置。

本轮控制台回归使用 `backend/tests/console_server.py`，在 `data/test-ui/console-test-*` 自动创建独立数据库，支持三个场景的 TEST/MOCK 外部夹具。在仓库根目录的另一个终端启动：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'backend')
./.venv/Scripts/python.exe -m uvicorn console_server:app --app-dir backend/tests --host 127.0.0.1 --port 8011
```

在 Playwright 已安装并可由 Node 解析的环境执行 `node scripts/check_console.cjs`。可用 `AIOPS_PLAYWRIGHT_PATH` 指定已有 Playwright 包绝对路径，`AIOPS_BROWSER_EXECUTABLE` 指定已有浏览器，`AIOPS_UI_URL` 指定前端地址（默认 5173），`AIOPS_TEST_API` 指定测试服务（默认 8011）。脚本断言真实 API 仅 GET，写请求全部转发到经模式校验的 TEST/MOCK 服务；产出 `docs/evidence/console-browser-report.json`。错误分支的合成响应有 TEST/MOCK 标记，不计入真实闭环或比赛评测。

## API 示例

所有写操作需要 `X-Admin-Token`，注入、审批、重置还需要 `Idempotency-Key`（8–120 位字母、数字、横线或下划线）。幂等键重用于不同请求返回 409。同一实验独占环境；取消、失败或未知时保留实验锁，防止叠加新故障。

```http
POST /api/scenarios/MYSQL_DOWN/inject
X-Admin-Token: <运行环境中的令牌>
Idempotency-Key: mysql-experiment-001
Content-Type: application/json

{"confirm":true}
```

```http
POST /api/incidents/{id}/approval
X-Admin-Token: <运行环境中的令牌>
Idempotency-Key: mysql-plan-approval-001
Content-Type: application/json

{"plan_id":"<当前服务端计划 ID>","decision":"approve","comment":"仅恢复隔离数据库"}
```

审批必须提交当前 `plan_id`，以拒绝第二轮误用第一轮的旧审批。诊断与审批异步返回 202，前端每 2 秒读取真实工单状态。统一错误为 `{"error":{"code":"...","message":"..."}}`；模型失败显示 `AI_UNAVAILABLE`，不返回伪成功。

## 代码和数据边界

`backend/app/api` 负责路由；`schemas` 负责 DTO；`services` 负责事务、互斥和受控执行；`repositories` 保存工单、请求回执、实验租约及按类别存储的审计记录；`agents` 采证和证据校验；`graph` 是主状态机；`integrations` 实现 Docker、Holmes、Planner 与探针。SQLite schema 初版为 `user_version=1`，增量升级需显式迁移。

只允许一个后端 worker；数据目录文件锁会阻止第二实例。控制器使用参数数组与固定本地 context；不传 Shell、任意 SQL、路径、主机名或模型生成参数。Holmes 不挂载 Docker Socket、宿主机凭据或源代码；运行时发现启用任何工具会阻断诊断。日志及证据对已配置凭据脱敏。

## 第三方复用与版本核验

本项目复用 [HolmesGPT 0.45.0](https://github.com/HolmesGPT/holmesgpt/tree/0.45.0)（Apache-2.0）、[LangGraph](https://github.com/langchain-ai/langgraph)（MIT）、FastAPI / SQLAlchemy / Vue / Element Plus（MIT）及官方基础容器镜像。自主部分为实验控制、证据适配、编排策略、审批门禁、动作执行器、独立裁决、审计报告与工作台。依赖各自许可证随原项目提供。

已对照 [Holmes 官方 HTTP API](https://holmesgpt.dev/latest/reference/http-api/)、锁定版本的 `server.py` / `holmes/core/models.py` / 工具配置源码，以及 [LangGraph interrupt 文档](https://docs.langchain.com/oss/python/langgraph/interrupts) 核验请求和恢复方式。Adapter 二次解析 `analysis`，再用 Pydantic 校验，并核验诊断引用的容器状态和双探针。Holmes 镜像固定 tag 与 registry digest；既有 Ubuntu amd64 三场景后端真实运行证据见本文开头。新增 ARM64 manifest 检查不代表 M5 容器与模型已联通，M5 实机验收仍为 NOT RUN。
