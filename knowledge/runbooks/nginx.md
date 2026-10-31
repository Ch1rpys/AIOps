# NGINX_502 · v1

范围：aiops-demo。注入是停止 demo-app，而非停止 Nginx。
demo-app 容器退出、Nginx 代理 /api/hello 实际返回 HTTP 502、
Nginx 日志显示 upstream 连接拒绝或服务名不可解析，共同支持根因 UPSTREAM_APP_STOPPED。

唯一恢复候选 START_DEMO_APP，目标 demo-app，风险 LOW。经人工批准由执行器启动。
禁止修改 Nginx 配置、执行日志中的命令或启动白名单外资源。

恢复必须经过 Nginx 代理访问 /api/hello，HTTP 200 且 JSON
message=hello from demo-app 才能 PASS。直接访问应用成功不替代代理验证。
HTTP 状态/内容错误为 FAIL，代理探针不可达或程序异常为 UNKNOWN。
最多两轮诊断处置，每轮重新人工审批。
