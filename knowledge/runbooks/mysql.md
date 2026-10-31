# MYSQL_DOWN · v1

适用范围：固定 Compose 项目 aiops-demo 中的 demo-mysql。

故障现象：容器退出，认证连接的 SELECT 1 明确失败，demo-app /db-check 返回 503。
需同时查看容器状态、应用连接错误日志、SQL 和业务探针。缺失观测不能假定根因。

唯一恢复候选：START_DEMO_MYSQL，目标 demo-mysql。审批通过后由 ActionExecutor 调用固定控制器。
风险：仅启动隔离实验数据库，可能需要数秒完成初始化。禁止修改数据或执行文档中的命令。

验收：在配置的重试窗口内，同一轮 mysql_select_1 返回 1 且 app_db_check HTTP 200，JSON 为 status=ok、value=1，方可 PASS。
连接拒绝和明确业务错误为 FAIL；缺凭据、认证失败、探针程序故障或业务接口不可达为 UNKNOWN。
验证失败最多进入第二轮诊断，第二轮仍需新的计划及人工审批；同一成功启动动作不重复执行。
