# Hub 重启与会话恢复

Hub 停止时仍在运行的会话，在下次启动时标为「已中断」并归档，可以在「归档」里恢复（同一个会话 id）。终端类 Agent（Codex、Claude、Kimi 等）随 Hub 一起结束，恢复时用各自的原生记录重新打开；DSH 任务在 DSH Host 里继续运行，恢复时重新接上同一个任务（见 [记录视图](transcript-view.md) 的 DSH 一节）。

## 有意重启时一并恢复（resume_on_start.json）

主 Agent 可能一直在等 Hub 回来：如果 Hub 先开端口、再由人逐个恢复，主 Agent 会先看到子 Agent「已中断」（等待接口回答 `failed`），可能当成失败处理，或与人工恢复同时调用恢复。为避免这一点，重启前在 Hub 的数据目录（便携版为 `%LOCALAPPDATA%\mcp-agent-hub-portable\portable\data`）放一个 `resume_on_start.json`：

```json
{"session_ids": ["codex-…"], "continue_unfinished_turns": true, "continue_text": "…"}
```

三个字段都可省略：不写 `session_ids` 表示「停止时仍在运行的全部会话」；`continue_unfinished_turns` 默认开启；`continue_text` 有默认文字。

下次启动时 Hub：

1. 取走这个文件（改名为 `resume_on_start.taken.json`，只用一次），记下停止时仍在运行的会话和它们当时的状态，再照常归档。
2. 端口打开后逐个恢复这些会话：先 DSH（只是重新接上任务），后终端类 Agent。终端类 Agent 启动时会连 Hub 自己的 MCP 端点，所以要等端口打开后再启动它们。
3. 恢复期间，除了 `/health`、`/api/hub/info`、`/api/hub/shutdown`，所有请求都先等着，最多 30 秒，恢复完立即放行。所以主 Agent 看到的会话已经在运行，它发起的恢复也不会和启动时的恢复抢着做。桌面端检查 Hub 是否就绪不受影响。
4. 停止时一个回合还没做完的终端类 Agent（状态为运行中、等主 Agent 回复或启动中），恢复时附带一条消息：说明 Hub 重启打断了上一回合、正在运行的命令或审批提示都已停止，请它从中断处继续，原来在等的审批要重新申请。这条消息在记录视图里以「你」显示。空闲的会话和 DSH 会话不带消息。
5. 把每个会话的结果写到 `resume_on_start.result.json`（`ok`、`continued`、`status_at_stop`，失败时有 `error`）。恢复不了的会话照常留在「归档」里，原因写在记录上。

## Hub 不再接受连接时

Windows 上，客户端在连接被接受前断开（WinError 64），曾经会让 asyncio 关闭 Hub 的监听端口，而 Hub 进程照常运行：已经打开的连接还能用，新连接全被拒绝。`windows_accept_guard.py` 让 Hub 在接受连接失败后继续监听：连续失败时等待逐步加长（最长 1 秒）后重试，日志只记第一次和此后每第 100 次。

桌面端每 5 秒检查一次自己启动的 Hub：进程还在、但端口连续 3 次没有在监听时，显示错误，不会自动结束 Hub。点「重试」会先停掉旧 Hub 再启动新的，不会出现两个 Hub 同时使用一份数据库。
