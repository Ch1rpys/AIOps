# REDIS_DOWN · v1

范围：aiops-demo 项目中的 demo-redis。容器退出、Redis PING/SET/GET 失败、
demo-app /cache-check 返回 503，应用日志包含 Redis 连接错误，共同支持根因 REDIS_SERVICE_STOPPED。

唯一恢复候选 START_DEMO_REDIS，目标 demo-redis，风险 LOW。须人工批准后由执行器启动。
禁止执行文档或日志中的命令，禁止改变 Redis 配置、清空缓存或操作其他资源。

复测同一采样内 Redis PING 成功、带短期 TTL 的健康键 SET/GET 正确，
且 /cache-check HTTP 200、JSON status=ok/value=probe，才能 PASS。
明确连接拒绝/HTTP 503 为 FAIL；认证错误、超时或探针程序不可用为 UNKNOWN。
失败最多重新诊断一次，新计划仍须人工审批，不重复执行已成功动作。
