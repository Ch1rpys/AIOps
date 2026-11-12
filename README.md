# AIOps 故障诊断与处置验证平台

日常操作、五种故障、审批复测、盲诊评测和接口说明见 [使用手册](docs/使用手册.md)。下面保留仓库协作和启动备忘；页面上的当前行为以使用手册为准。

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

## 本机双击启动

Windows 双击仓库根目录的 `start.cmd`。macOS 双击 `start-demo.command`。脚本在本机安装锁定的 Python 与前端依赖，启动 Docker 里的 Holmes、四个 demo，并尝试再拉四个影子容器（项目 `aiops-twin`，端口 23306 / 26379 / 28000 / 28080），再打开 <http://127.0.0.1:5173>。影子启动失败时旧六场景仍可用，但工作台不得声称做过反事实试修。不连接虚拟机，也不需要另开终端。本机同时跑八个容器时建议 16GB 内存。

首次运行需要已经安装的 Docker Desktop，以及联网下载工具和镜像。工具装在项目内的 `.tools/`，不提交。已有 demo 容器保持原状态，避免把待审批故障自动恢复。没有 `.env` 时会生成本机管理员口令和实验数据库密码；已经填写的模型地址和密钥不会被覆盖。模型密钥仍为空时页面也会打开，诊断和识图要等密钥写进 `.env`。日志在 `data/backend.log` 与 `data/frontend.log`。8000 或 5173 端口被占用时，脚本会停住并请你先关掉上一次的演示窗口。

## Ubuntu 部署

下面是在 Ubuntu 本机分终端启动的方式，不是 Windows 或 macOS 的演示入口。演示请用上一节的双击脚本。

首次没有 demo 容器时创建四个 demo；已有 demo 一律保留当前状态，避免把待审批故障自动恢复。Holmes 健康检查和 Planner 配置检查不代表真实模型诊断通过。启动失败返回非零；日志为 `data/backend.log`、`data/frontend.log`。若本机 5173 / 8000 已被占用，先关掉旧进程再启动。

在自己的 Ubuntu 项目目录使用 Python 3.11、uv、Node 22.12+，首次通过 `uv sync --frozen` 与 `frontend` 中的 `npm ci` 安装锁定依赖。Linux 启动脚本：

```bash
export PATH="$HOME/.local/bin:$PATH"
bash scripts/start_lab.sh
bash scripts/start_backend.sh
# 另一终端执行：
bash scripts/start_frontend.sh
```

Linux 实验启动脚本默认启动四个 demo；Holmes 使用同一 Compose 文件的 `holmes` 服务单独启动。服务均保持 loopback 绑定，Vue 代理同机 FastAPI。私钥、管理员令牌和数据库密码不随代码同步。恢复期间的每次探针采样均保存在实际工单中。

DeepSeek 配置采用 `HOLMES_MODEL=deepseek/deepseek-flash`、`PLANNER_MODEL=deepseek-flash`、`PLANNER_URL=https://api.deepseek.com/v1`；`DEEPSEEK_API` 和 `PLANNER_API_KEY` 通过受限 .env 注入，两个 JSON_MODE 均设为 `json_object`。此模式遵循 [DeepSeek 官方 JSON Output](https://api-docs.deepseek.com/guides/json_mode/)，提示中携带 Schema，结果仍用原 Pydantic 契约验证，不放宽动作白名单。默认其他提供商仍使用 `json_schema`。

## 本轮可用范围

- `/lab`：三场景参数化注入、目标确认、互斥与基线门禁，按场景保留网络重试幂等键。
- `/environment`：四容器实际状态、平台/Docker/Holmes/Planner 自检、当前实验租约及鉴权确认重置；`?tab=probes` 查看基线探针完整返回。Planner 配置就绪不等于真实模型连通，仍如实显示 UNKNOWN。
- `/incidents/:id`：服务端工单、按轮次保存的节点记录、诊断引用的实际证据、右侧审批、执行回执及全部复测采样。当前环境采样与工单历史分别标记时间。
- `/incidents`：场景/状态服务端筛选与分页、工单详情和真实 Markdown 报告下载；左侧保留最近六条工单与错误反馈。
- `/evaluation`：盲诊评测独立页面。只做诊断对照，不产生任何处置提议；默认不显示真值，切换「评测视图」才显示（需管理员令牌）。未见故障按校准签名单独计分。
- `/compiler`：配方浏览器。holdout 冻结在 `knowledge/compiler/holdout.json`。
- `/twin`：影子世界专页。独立 Compose 项目，Holmes 不在这里。
- `/incidents/:id` 新增两块：**服务影响图**（严格区分已观测异常 / 配置依赖 / 推断传播三类关系）与**复盘摘要**（何时发生、影响哪个接口、根因及引用、批准动作、修复前后探针、未解决项），摘要只投影已保存记录，不重新执行容器动作。
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
./scripts/run_acceptance.ps1 -Rounds 3
```

脚本输出 PASS/FAIL，退出码成功 0、失败非零。原始 HTTP 响应写入 `data/acceptance/*.json`，每轮另存 `data/acceptance/<工单>.md`（Markdown 报告）与 `<工单>.diagnosis.json`（模型原始结论）。某一轮失败会保留失败工单并直接失败，不重跑到全绿。基线不健康时停止，不操作其他 Docker 服务。重置只经控制台确认和认证接口；停止演示服务可运行 `./scripts/stop_lab.ps1`，不会删除容器、数据卷或镜像。

### 盲诊评测（M1）

盲诊是**独立链路**：只诊断、不提议处置、不调用规划器，也拿不到执行器入口。运行：

```bash
./.venv/bin/python scripts/run_evaluation.py --rounds 3
```

- 3 场景 × 3 轮 = 9 次盲诊；每轮把 A、B、C 三臂放在**同一份证据快照**上对照，快照指纹相同才可比。
- C 臂 = B 臂 + 同一份快照里的视觉证据：截图先经 `POST /api/evaluation/attachments` 上传并识图，再随实验带入快照。**C 臂没有截图会被明确拒绝**（不静默退化成 B 臂 —— 那是假对照）。
- 逐条人工复核：先看证据独立判断根因，再揭晓系统判卷；复核结论写进 `data/evaluation/blind-*.md`。
- 真值只落在 `evaluation_runs.truth_scenario`，接口默认不返回；`GET /api/evaluation/*?view=evaluation` 需要管理员令牌。
- 评测与工作台共用同一个四容器环境，**互斥**：评测进行中工作台注入会被拒绝，工作台实验进行中评测也会被拒绝。
- 预算是上限不是配额：**每个实验臂各一份**，模型调用 ≤3 次、总耗时 ≤45 秒（评测专用较短超时保证单次调用不会突破总预算）；超限的实验会被终止并单独记录，不混入正确率分母。两臂共用的快照采集单列为「采证时间」。
- 时间按阶段拆分显示（模型 / 采证 / 容器启停），token 取不到就标「未知」；失败的实验不报「0 毫秒」。端到端指标（错误处置提议数、拦截率、恢复成功率）与纯诊断对照分开呈现，纯诊断下显示「不适用」而不是 0。
- 9 次实验是**复现基线**，不构成统计显著性；根因枚举收窄了假设空间，报告里必须显式声明。

### 受限主动补采、影响图与稳定窗口

- **补采（M3）**：模型只能从已注册的检查 ID（`mysql_select_1`、`app_db_check`、`redis_roundtrip`、`app_cache_check`、`nginx_hello`、`app_health`）里选要补什么，并说明理由；最多 2 轮、单轮 2 项、总耗时预算封顶，超限如实记录原因而不静默截断。页面展示「为什么需要这项检查」与「新增证据排除了什么」，不展示模型隐藏思考过程。
- **拓扑（M4）**：`knowledge/topology.yaml` 是业务拓扑的唯一数据源，通用 Runbook 与影响图都从它生成（`scripts/generate_runbooks.py` 重新生成，测试校验不漂移）。启动顺序只标注为「配置依赖」，不等同于业务因果。
- **视觉证据（M2）**：工单页可上传真实错误页面或运维截图（PNG / JPEG / WebP，5MB 以内），后台受控存储并提取观测。**上传时间不能代替截图实际发生时间**：图里没有时间戳就标「时间未知」；截图显示异常但当前探针健康时提示「时间或证据冲突」并补查，不触发恢复。图片本身不决定恢复 PASS。未配置视觉模型时健康检查显示 UNKNOWN，不做降级伪装。
- **稳定窗口（M5）**：默认关闭；开启后按「连续健康次数 + 观察窗口」判定，窗口内再次失败显示「恢复不稳定」，失败采样不会被删除，未知不等于失败。窗口内健康只代表这一段时间的观测，不代表永久不再复发。

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

所有写操作需要 `X-Admin-Token`，注入、审批、重置、评测触发还需要 `Idempotency-Key`（8–120 位字母、数字、横线或下划线）。幂等键重用于不同请求返回 409。同一实验独占环境；取消、失败或未知时保留实验锁，防止叠加新故障。盲诊评测使用独立的评测租约，与工作台实验锁互斥：评测进行中注入与重置会被拒绝，工作台实验进行中评测也会被拒绝。

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

`backend/app/api` 负责路由；`schemas` 负责 DTO；`services` 负责事务、互斥和受控执行；`repositories` 保存工单、请求回执、实验租约、评测记录及按类别存储的审计记录；`agents` 采证和证据校验；`graph` 是主状态机；`integrations` 实现 Docker、Holmes、Planner、视觉模型与探针。SQLite schema 当前为 `user_version=2`（v1 → v2 新增评测表），增量升级走显式迁移分支，版本号只升不降；旧二进制读到未知版本会直接拒绝启动。

只允许一个后端 worker；数据目录文件锁会阻止第二实例。控制器使用参数数组与固定本地 context；不传 Shell、任意 SQL、路径、主机名或模型生成参数。Holmes 不挂载 Docker Socket、宿主机凭据或源代码；运行时发现启用任何工具会阻断诊断。日志及证据对已配置凭据脱敏。

## 第三方复用与版本核验

本项目复用 [HolmesGPT 0.45.0](https://github.com/HolmesGPT/holmesgpt/tree/0.45.0)（Apache-2.0）、[LangGraph](https://github.com/langchain-ai/langgraph)（MIT）、FastAPI / SQLAlchemy / Vue / Element Plus（MIT）及官方基础容器镜像。自主部分为实验控制、证据适配、编排策略、审批门禁、动作执行器、独立裁决、审计报告与工作台。依赖各自许可证随原项目提供。

已对照 [Holmes 官方 HTTP API](https://holmesgpt.dev/latest/reference/http-api/)、锁定版本的 `server.py` / `holmes/core/models.py` / 工具配置源码，以及 [LangGraph interrupt 文档](https://docs.langchain.com/oss/python/langgraph/interrupts) 核验请求和恢复方式。Adapter 二次解析 `analysis`，再用 Pydantic 校验，并核验诊断引用的容器状态和双探针。Holmes 镜像固定 tag 与 registry digest；既有 Ubuntu amd64 三场景后端真实运行证据见本文开头。新增 ARM64 manifest 检查不代表 M5 容器与模型已联通，M5 实机验收仍为 NOT RUN。
