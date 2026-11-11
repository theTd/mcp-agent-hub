# Sub Agent 及时关闭与超时回收

主 Agent 从 `wait_url` 收到结果、不再需要复用 Sub Agent 时，应立即调用 `terminate_subagent(session_id)`。任务完成只代表本轮结束，TUI 进程通常仍在运行。结束主任务前应关闭自己启动且已不再需要的所有 Sub Agent。此提醒包含在 MCP 服务说明和路由指南中。

## 自动关闭

默认对主 Agent 启动的会话启用 **60 分钟**闲置超时。计时从本轮完成（`completed`）或进程结束开始，而非从启动或最后一次终端输出开始。再次派发任务后重新计时；查询状态、收取结果、重复完成通知和 TUI 刷新不延长时限。

- 执行中、启动中以及等待问题或审批的会话不自动关闭；有排队输入时也不关闭。
- 控制台手动启动的会话、来源不明的旧会话不受影响。
- 每 30 秒检查一次，到期后通过现有关闭流程终止进程树、断开控制台连接并归档；生命周期事件会注明 Hub 自动关闭。
- 已保存的答案、工具记录和终端历史仍可查询，符合原生恢复条件时可通过 `resume_subagent` 恢复。恢复后重新计时。
- Hub 停止时取消检查任务；重启沿用现有归档规则处理之前的会话，不会额外恢复进程来计时。

## 配置

控制台「Profile 与模型设置 → 路由总则」中的「Sub Agent 完成后自动关闭（分钟）」可修改时限，默认 60，0 表示禁用。点击「保存设置」后立即应用于已有和后续符合条件的会话；缩短时限可能使已闲置很久的会话在下一次检查时关闭。

设置保存在 `agent_profiles.json` 顶层，不更改各 Profile，也不使用浏览器偏好：

```json
{
  "subagent_idle_timeout_minutes": 60,
  "profiles": {}
}
```

HTTP `GET /api/profiles` 返回当前值；`POST /api/routing-guide` 与路由总则一并保存：

```json
{
  "global_routing_instructions": "按任务需要选择 Sub Agent。",
  "subagent_idle_timeout_minutes": 90
}
```

只接受非负整数。旧调用方不传该字段时保留已有值。自动回收只是兜底，主 Agent 仍应及时关闭用完的 Sub Agent。
