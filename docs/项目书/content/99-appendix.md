# 附录 A 接口与错误码

平台共 39 个 REST 路由：25 个 GET、14 个 POST。只读接口不要求令牌；写操作要求 `X-Admin-Token`；注入、重置、审批、评测要求 `Idempotency-Key`（8–120 位字母、数字、横线或下划线）。下面按用途分组，完整字段以运行中的 `/docs` 为准。

| 分组 | 方法与路径 | 作用 | 鉴权 / 幂等 |
| --- | --- | --- | --- |
| 健康 | `GET /api/health` | 平台、Docker、Holmes、Planner、Vision 自检 | 只读 |
| 环境 | `GET /api/lab/status` | 四容器、6 探针、当前租约 | 只读 |
| 环境 | `POST /api/lab/reset` | 收回 Nginx / 应用模式、启动四服务、跑基线 | 令牌 + 幂等 |
| 目录 | `GET /api/action-catalog` | 目录条数与说明 | 只读 |
| 场景 | `GET /api/scenarios` | 六场景名称、目标、判据 | 只读 |
| 注入 | `POST /api/scenarios/{scenario}/inject` | 注入规范场景并确认故障 | 令牌 + 幂等 |
| 注入 | `POST /api/scenarios/compile/inject` | 抽 seen 配方或指定 `rec_` 双侧注入 | 令牌 + 幂等 |
| 工单 | `GET /api/incidents` | 按状态/场景筛选、分页 | 只读 |
| 工单 | `GET /api/incidents/{id}` | 工单详情与审计记录 | 只读 |
| 工单 | `GET /api/incidents/{id}/steps` | 只要步骤 | 只读 |
| 工单 | `GET /api/incidents/{id}/evidence` | 只要证据 | 只读 |
| 工单 | `GET /api/incidents/{id}/verification` | 只要复测 | 只读 |
| 工单 | `GET /api/incidents/{id}/report` | 工单 JSON（不是 Markdown） | 只读 |
| 工单 | `GET /api/incidents/{id}/report.md` | 处理报告 Markdown | 只读 |
| 工单 | `GET /api/incidents/{id}/review` | 复盘 JSON | 只读 |
| 工单 | `GET /api/incidents/{id}/review.md` | 复盘 Markdown | 只读 |
| 工单 | `GET /api/incidents/{id}/talk.md` | 三分钟讲稿 | 只读 |
| 工单 | `GET /api/incidents/{id}/impact` | 影响图（三类关系） | 只读 |
| 工单 | `POST /api/incidents/{id}/diagnose` | 开始诊断，202 已接受 | 令牌 |
| 工单 | `POST /api/incidents/{id}/approval` | 批准/拒绝当前 `plan_id` | 令牌 + 幂等 |
| 附件 | `POST /api/incidents/{id}/attachments` | 上传截图并识图 | 令牌 |
| 附件 | `GET /api/incidents/{id}/attachments` | 截图元数据 | 只读 |
| 评测 | `POST /api/evaluation/runs` | 触发盲诊实验，202 已接受 | 令牌 + 幂等 |
| 评测 | `GET /api/evaluation/runs` | 实验列表 | 只读 |
| 评测 | `GET /api/evaluation/runs/{id}` | 单条实验 | 只读 |
| 评测 | `GET /api/evaluation/summary` | 汇总（不含真值） | 只读 |
| 评测 | `GET /api/evaluation/unseen/summary` | 未见故障汇总（不含原语） | 只读 |
| 评测 | `POST /api/evaluation/attachments` | 上传 C 臂截图，不建工单 | 令牌 |
| 世界 | `GET /api/worlds` | 真环境与影子快照 | 只读 |
| 世界 | `GET /api/worlds/{name}/logs/{service}` | 指定世界的服务日志 | 只读 |
| 影子 | `GET /api/twin/trials` | 最近试修 | 只读 |
| 影子 | `POST /api/twin/ensure` | 拉起影子 | 令牌 |
| 影子 | `POST /api/twin/reset` | 重置影子基线 | 令牌 |
| 影子 | `POST /api/twin/trial` | 手动试修 | 令牌 |
| 影子 | `POST /api/twin/search` | R 臂目录搜索 | 令牌 |
| 编译 | `GET /api/compiler/recipes` | 配方列表；`view=holdout` 要令牌 | 只读 |
| 编译 | `GET /api/compiler/recipes/{id}` | 配方详情；holdout 无令牌不返回原语 | 只读 |
| 编译 | `POST /api/compiler/calibrate` | 影子校准 | 令牌 |
| 编译 | `POST /api/compiler/freeze` | 冻结 holdout（默认关闭） | 令牌 |

统一错误体是 `{"error": {"code": "...", "message": "..."}}`。下面是最常遇到的错误码。

| 错误码 | 含义 | 处理 |
| --- | --- | --- |
| `FORBIDDEN` | 没带令牌或令牌不匹配 | 从 `.env` 重新复制 `ADMIN_TOKEN` |
| `ADMIN_NOT_CONFIGURED` | `.env` 没有管理员令牌 | 填一个随机值并重启 API |
| `BASELINE_UNHEALTHY` | 四容器或基线探针不齐 | 看环境页，修 Docker 或重置 |
| `LAB_BUSY` | 工作台占着环境 | 完成当前工单或重置 |
| `EVALUATION_BUSY` | 评测还在跑 | 等评测结束 |
| `INVALID_STATE` | 工单还不能诊断，或批准的不是当前计划 | 看状态是否为 `INJECTED` / `WAITING_APPROVAL` |
| `ALREADY_DECIDED` | 这个计划已批准/拒绝过 | 用新计划编号 |
| `IDEMPOTENCY_CONFLICT` | 幂等键被用于另一个请求 | 换新键 |
| `AI_UNAVAILABLE` | 模型超时、密钥错或 Holmes 未起 | 看 `data/backend.log` 与 Holmes 健康 |
| `AI_INVALID_RESPONSE` | 模型输出不合契约，且探针未证实故障 | 看工单里的 `raw_model_output` |
| `HOLMES_VERSION_MISMATCH` | Holmes 不是 0.45.0 | 用仓库 Compose 重建 |
| `EXECUTION_UNCERTAIN` | 动作可能已执行，不能自动重放 | 检查容器后重置 |
| `TWIN_UNAVAILABLE` | 影子没起来或试修失败 | 工作台旧场景仍可用；不要声称做过试修 |
| `TWIN_BUSY` | 世界锁被另一场试修占用 | 等当前试修结束 |
| `TWIN_DIVERGENCE` | 两界探针不一致 | 该配方不能用于宣传预测证书 |
| `HOLDOUT_FORBIDDEN_IN_LAB` | 想把 holdout 拿到工作台 | 换 seen 或走评测页 |
| `EVALUATION_BUDGET_EXCEEDED` | 评测超调用次数/时长 | 记录为超限样本，不进正确率分母 |
| `SCHEMA_VERSION_MISMATCH` | 数据库版本不兼容 | 不要跨版本复用；新环境可移走数据库 |
| `RECIPE_INVALID` | 配方 DSL 或编号不对 | 用 `/compiler` 列出的 `rec_` 编号 |

# 附录 B 复现与验收命令

以下命令都在仓库根目录执行，Python 3.11、Node 22.12+、Docker Desktop 已就绪。密钥只从 `.env` 读取，不要写进命令历史或报告。

```bash
# 依赖
uv sync --frozen --cache-dir .cache/uv
cd frontend && npm ci --cache ../.cache/npm --no-audit --no-fund && cd ..

# 本机双击入口（macOS）
./start-demo.command
# Windows：双击 start.cmd

# 后端测试与静态检查
./.venv/bin/pytest -q
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .

# 前端检查
cd frontend
npm run lint
npm run typecheck
npm run build
cd ..

# holdout 冻结校验
PYTHONPATH=backend ./.venv/bin/python scripts/check_holdout.py

# 真实闭环验收（逐轮人工批准）
./.venv/bin/python scripts/run_acceptance.py --preflight
./.venv/bin/python scripts/run_acceptance.py --rounds 1

# 盲诊复现基线（需要真实模型密钥）
./.venv/bin/python scripts/run_evaluation.py --rounds 3
./.venv/bin/python scripts/run_unseen_evaluation.py

# 影子验收（需要 8 个容器）
./.venv/bin/python scripts/run_twin_acceptance.py
```

一次最小 API 闭环（把 `<ADMIN_TOKEN>` 换成 `.env` 里的值）：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/scenarios/MYSQL_DOWN/inject \
  -H "X-Admin-Token: <ADMIN_TOKEN>" \
  -H "Idempotency-Key: mysql-experiment-001" \
  -H "Content-Type: application/json" \
  -d '{"confirm": true}'

curl -sS -X POST http://127.0.0.1:8000/api/incidents/<工单编号>/diagnose \
  -H "X-Admin-Token: <ADMIN_TOKEN>"

curl -sS -X POST http://127.0.0.1:8000/api/incidents/<工单编号>/approval \
  -H "X-Admin-Token: <ADMIN_TOKEN>" \
  -H "Idempotency-Key: mysql-plan-approval-001" \
  -H "Content-Type: application/json" \
  -d '{"plan_id": "<当前 plan_id>", "decision": "approve", "comment": "仅恢复隔离数据库"}'
```

审批必须提交当前 `plan_id`；上一轮的编号不能复用。诊断和审批返回 202 表示“已接受”，真正执行在后台；前端每 2 秒轮询真实状态。

验收脚本会留下三类产物，失败样本不删除：

- `data/acceptance/*.json`：每一步的原始 HTTP 响应，包含状态码和错误体；
- `data/acceptance/<工单>.md`：该轮工单的 Markdown 报告；
- `data/acceptance/<工单>.diagnosis.json`：模型原始结论，便于判断是格式问题还是内容问题。

某一轮失败会保留失败工单并直接返回非零，不重跑到全绿；重跑得到的是另一张工单，不会覆盖历史。

读验收输出时注意三点：

- 退出码 0 且每一轮都 PASS 才算通过；某一轮失败时脚本返回非零，不重跑到全绿；
- 报告是投影，原始 HTTP 响应在 `*.json`；两者不一致时以原始响应为准；
- `TEST/MOCK` 工单只用于自动化测试，不计入真实闭环或比赛成绩。

# 附录 C 术语表

| 术语 | 含义 |
| --- | --- |
| 配方（recipe） | 1–4 条原语组成的故障定义，编号由规范哈希决定 |
| 原语（primitive） | 编译器可执行的最小故障操作，如停容器、改 Nginx 上游、设 Redis maxmemory |
| 校准（calibration） | 在影子里注入配方、采全套探针、把实际红/绿写成签名 |
| 签名（signature） | `failing_checks` / `passing_checks` / `unknown_checks` / `affected_services` 的真值集合 |
| holdout | 冻结的评测配方集合，工作台不可注入，盲诊只从这里抽题 |
| seen | 校准池中未进入 holdout 的配方，工作台可随机注入 |
| 影子世界（twin） | 独立 Compose 项目、独立端口/卷/配置的四个容器，用于校准与试修 |
| 预测证书 | 真环境复测后，试修预测与实测探针逐项对照的匹配率 |
| 快照指纹 | 对同一份证据做 JSON 规范化后的 SHA-256，A/B/C 复用的技术保证 |
| 租约（lease） | 工作台实验或评测对环境的独占标记；评测租约 TTL 900 秒 |
| 幂等键 | 写请求的请求指纹；同键同请求复用响应，同键异请求 409 |
| 证据完整性 | 引用的证据 id 存在，且覆盖容器状态与全部必需探针 |
| 执行许可 | 证据充分、根因命中场景、实测故障成立，才允许进入审批 |
| R 臂 | 影子中按目录搜索恢复动作的非模型基线，不进盲诊 |
| L 臂 | 对抗样本中的闭环规划臂，只评分、不执行 |
| 漂移（drift） | 现场签名与冻结签名不一致；剔除分母，不算模型答错 |

# 附录 D 第三方组件与许可

平台复用成熟组件，自主部分是实验控制、证据适配、编排策略、审批门禁、动作执行器、独立裁决、审计报告与工作台。主要第三方如下：

| 组件 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| HolmesGPT | 0.45.0 | Apache-2.0 | 诊断推理；关闭全部工具，只读证据 |
| LangGraph | 1.2.19 | MIT | 状态机、`interrupt`、检查点 |
| FastAPI | 0.162.0 | MIT | REST API |
| Pydantic | 2.14.2 | MIT | 契约校验 |
| SQLAlchemy | 2.0.55 | MIT | ORM 与事务 |
| Vue | 3.5.44 | MIT | 前端框架 |
| Element Plus | 2.14.7 | MIT | UI 组件 |
| Vite | 7.3.7 | MIT | 前端构建 |
| MySQL | 8.4.7 | GPLv2（含 FOSS 例外）/ 商业 | 隔离数据库镜像 |
| Nginx | 官方镜像 | BSD-2-Clause | 反向代理 |
| Redis | 官方镜像 | BSD-3-Clause | 隔离缓存 |

参考链接：

- HolmesGPT 0.45.0：<https://github.com/HolmesGPT/holmesgpt/tree/0.45.0>
- LangGraph：<https://github.com/langchain-ai/langgraph>
- FastAPI：<https://fastapi.tiangolo.com>
- Vue：<https://vuejs.org>
- Element Plus：<https://element-plus.org>
- Docker Compose：<https://docs.docker.com/compose/>
- Nginx：<https://nginx.org>
- Redis：<https://github.com/redis/redis>

依赖版本以 `uv.lock` 与 `frontend/package-lock.json` 为准；本文列的是本机实际安装版本。升级第三方组件前，应先跑 holdout 校验、后端测试和前端构建，再重新校准受影响的配方。

使用边界：

- HolmesGPT 只作为诊断推理服务，不挂载 Docker Socket、宿主机凭据或源代码；运行时发现启用工具会阻断诊断。
- MySQL / Redis / Nginx 镜像只用于隔离实验，不对外提供服务，也不复用任何生产凭据。
- 第三方组件升级前，先跑 holdout 校验、后端测试与前端构建；原语表或镜像变化后，必须重新校准受影响的配方。
