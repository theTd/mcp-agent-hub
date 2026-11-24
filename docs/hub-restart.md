# Hub 重启与会话恢复

Hub 停止时仍在运行的会话会被中断。优雅重启把「停 Hub → 拉起新 Hub → 恢复会话」做成一个动作，并保证主 Agent 的等待不会被这次重启判成失败。

## 三个入口

- **托盘菜单**（桌面版）：一键「重启 Hub」，桌面壳识别计划内重启，不弹错误页，自动拉起新 Hub。
- **`tools/hub_graceful_restart.py`**（便携版自带，标准库，Python 3.8+）：`status`、`restart`、`replace-exe` 三个子命令，输出一个 JSON。
- **HTTP**：`POST /api/hub/graceful-restart` 与 `GET /api/hub/restart-status`。

重启接口与停止接口一样校验 `x-hub-shutdown-token`：没启用 token 返回 404，token 无效 403。**不提供无鉴权的重启入口**，Web 控制台也没有重启按钮。

## 重启做了什么

`POST /api/hub/graceful-restart` 的请求体（字段都可选）：

```json
{
  "mode": "hub",
  "restart_id": "可选 uuid，省略时服务端生成",
  "continue_unfinished_turns": true,
  "resume_crashed": false,
  "session_ids": ["可选显式列表"],
  "continue_session_ids": ["可选显式列表"],
  "continue_text": "可选覆盖文案"
}
```

`mode` 只能是 `hub`（默认）或 `replace-exe`，其他值 400。Hub 已经在关闭/重启中时返回 `409 {"detail": "Hub 已在关闭或重启中"}`。

应答是 **202**，且在应答发出之前已经完成快照和落盘：

1. 同步快照当前存活会话（`{sid: {"engine": …, "status": …}}`，状态是它真实的 `running` / `completed` / `waiting_parent`，不是一律 `interrupted`）；
2. 原子写入 `<data_dir>/resume_on_start.json`（临时文件 + `os.replace`）；
3. 标记这次停止是计划内重启；
4. 后台任务执行优雅关停（和 Ctrl+C 同一条路径），先归档、唤醒长轮询，再结束 HTTP/MCP。

```json
{
  "ok": true, "restart_id": "…", "mode": "hub",
  "pid": 12345, "port": 4444, "started_at": "…",
  "sessions": {"codex-…": {"engine": "codex", "status": "running"}},
  "session_count": 1
}
```

旧的 `POST /api/hub/shutdown` 保持最终停止的语义；带 JSON body 且 `for_restart` 为 `true` 时，走与上面完全相同的准备路径（200，同样的 payload）。不带这个字段的请求（包括空 body）与以前一样是最终停止。

## 会话怎么恢复

下次启动时 Hub 取走 `resume_on_start.json`（改名为 `resume_on_start.taken.json`，只用一次），等端口打开后逐个恢复：

- **默认只恢复发起重启时存活的会话**。历史 `crashed` 会话不会因为一次重启被拉起来（`live_only_opt_in_crashed`）：只有显式列在 `session_ids` 里，或传 `resume_crashed: true`（扫描库里的 `crashed` 行），才会尝试恢复，并且仍要求引擎有恢复配方。
- 更早一次启动已归档为「已中断」、之后没人恢复过的会话，被列名时也会恢复。
- 停止时回合没做完的终端类 Agent（状态 `starting` / `running` / `waiting_parent`）恢复后收到一条继续消息；空闲的（`completed`）不带消息。消息内容可用 `continue_text` 覆盖，`continue_unfinished_turns: false` 则一律不发。
- 引擎不支持自动恢复（`pwsh`、`generic`）的会话记为一个 **skip**，不算失败。
- 恢复期间，除 `/health`、`/api/hub/info`、`/api/hub/shutdown`、`/api/hub/graceful-restart`、`/api/hub/restart-status` 以外的请求先等最多 30 秒；`/api/wait/` 长轮询改为返回 503 + `Retry-After` 让调用方重试。
- 每个会话的结果写进 `<data_dir>/resume_on_start.result.json`（`ok`、`continued`、`status_at_stop`，失败有 `error`）。

### 等待句柄跨重启有效

`wait_handle` 在重启前后始终有效：

- 计划内重启不会把等待结算成失败：Hub 启动时修掉 inbox 里残留的 `hub_shutdown` 事件，句柄保持有效，恢复后的会话照常往里报告。
- 恢复失败的会话会主动发一个 `restart_resume_failed` 错误事件，父 Agent 立刻被唤醒，而不是一直等一个永远不会回报的 worker。
- `tools/hub_wait.py` 对 503/`superseded` 等重启期应答本来就会重试，所以父 Agent 的等待命令不需要重跑。

## 状态：`GET /api/hub/restart-status`

只读，不需要 token，**不含任何密钥**；豁免关停 gate 与恢复 gate，Hub 正在停止或恢复时也答得出来。

```json
{
  "ok": true,
  "phase": "idle | stopping | resuming | ready | failed",
  "restart_id": "…", "mode": "hub", "requested_at": 1795470000.0,
  "started_at": "…", "port": 4444, "pid": 12345,
  "target_count": 3, "resumed_count": 3, "skipped_count": 0, "failed_count": 0,
  "result": null
}
```

- `idle`：冷启动，没有 `resume_on_start.json`（即使磁盘上留着上一次的 result 文件也仍是 `idle`）。
- `stopping`：重启请求已被接受，Hub 正在停。
- `resuming`：新 Hub 正在执行恢复，四个计数实时更新。
- `ready`：全部目标都有答案且没有失败（skip 可以大于 0）。
- `failed`：有会话恢复失败，或这一趟恢复没跑完。`result.sessions` 列出每个会话的明细。

## `tools/hub_graceful_restart.py`

便携版 payload 自带，纯标准库，绕过系统代理，只输出一个 UTF-8 JSON。退出码：`0` 正常（skip 不算失败），`2` 重启完成但有会话没恢复，`1` 拒绝或失败。

### `status`

```powershell
python tools\hub_graceful_restart.py status [--portable-root PATH] [--port N]
```

只读，什么都不改。输出 `launch.json`（**脱敏：只报 `shutdown_token_set`，绝不打印 token**）、`/api/hub/info`、`/health`、`/api/hub/restart-status`、`/api/sessions`（会话总数与活跃列表）、仍然有效的 `restart_plan.json`，以及最近一次 `resume_on_start.result.json` / `restart_plan.result.json`。

### `restart`

```powershell
python tools\hub_graceful_restart.py restart [--no-continue] [--resume-crashed] `
    [--session-id SID] [--timeout-seconds 300] [--no-wait]
```

1. 读 `<portable_root>/launch.json`（`port`、`pid`、`creation_time`、`app_pid`、`app_creation_time`、`data_dir`、`shutdown_token`）；
2. 发现未过期的 `restart_plan.json` 就拒绝，先不动任何东西；
3. 写 `<portable_root>/restart_plan.json`（`mode="hub"`，有效期 600 秒），桌面壳 `watch()` 据此知道这是计划内重启；
4. `POST /api/hub/graceful-restart`（带 token）；请求失败就删掉刚写的 plan 并返回 1；
5. 不传 `--no-wait` 时等旧 Hub 退出、新 Hub 起来（`started_at` 变了），再轮询 `/api/hub/restart-status` 到 `ready`/`failed`（默认超时 300 秒）。完成后删掉自己写的 plan。

### `replace-exe`

```powershell
python tools\hub_graceful_restart.py replace-exe <new_exe> [--target-exe PATH] `
    [--sha256 HEX] [--portable-root PATH] [--no-continue] [--resume-crashed] `
    [--timeout-seconds 600] [--dry-run]
```

**预检全部在停止任何东西之前完成，任何一项失败都立即退出、绝不停止旧 Hub：**

1. 新 EXE 必须存在；算 SHA-256，与 `--sha256` 比对，没传就找 `<new_exe>.sha256` 自动对账；
2. 运行 `<new_exe> --print-build-info`，必须是 `portable: true` 且输出的 `exe_sha256` 与文件一致（`--skip-build-probe` 仅测试用）；
3. 要替换的 EXE 默认按 `app_pid` 用 `QueryFullProcessImageNameW` 查真实路径（可用 `--target-exe` 覆盖）；拒绝新旧同一个文件，拒绝 `desktop\dist\` 里的构建产物；
4. 没有未过期的 `restart_plan.json`；
5. `--dry-run` 只打印通过预检的计划并退出 0，不写文件、不碰进程。

**执行阶段：**

1. 算旧 EXE 的 SHA-256；把新 EXE 复制成目标同目录、同卷的 `.hub-update-<nonce>.exe` 并复核 SHA-256；再复制成 `%TEMP%\hub-update-helper-<nonce>.exe`；
2. 原子写 `<portable_root>/restart_plan.json`（`mode="replace-exe"`）；
3. `POST /api/hub/graceful-restart`（`mode="replace-exe"`）；失败就清理暂存文件、helper 和 plan 后报错；
4. 用 `ShellExecuteW` 拉起 helper（脱离 Agent 的 Job Object，父进程结束也不会被带走），参数 `--apply-update "<plan.json>"`；
5. 轮询 `restart_plan.result.json` + `/api/hub/info` + `/api/hub/restart-status`（默认最多 600 秒，覆盖首次建 venv），核对新 Hub 的 `started_at` 变了、目标 EXE 的 SHA-256 已是新值，输出最终 JSON；通过后删掉自己写的 plan（`restart_plan.result.json` 保留作为记录）。

helper 等待旧 Hub 退出后 `ReplaceFileW`；替换失败会回滚，旧文件保留为 `<target_exe>.bak`。

### `restart_plan.json` 字段

CLI 与桌面壳、升级 helper 共用这一份计划（`<portable_root>/restart_plan.json`）：

| 字段 | 含义 |
|---|---|
| `version` | 1 |
| `restart_id` | 与请求里的 id 一致 |
| `mode` | `hub` 或 `replace-exe` |
| `requested_at` / `expires_at` | 写入时间与该计划的有效期（600 秒） |
| `port` | Hub 端口 |
| `app_pid` / `app_creation_time` | 桌面 App 进程与它的创建时间 |
| `hub_pid` / `hub_creation_time` | 旧 Hub 进程与它的创建时间 |
| `data_dir` | Hub 数据目录 |
| `target_exe` | 被替换的旧 EXE |
| `old_exe_sha256` / `new_exe` / `new_exe_sha256` | 换 EXE 的校验信息 |
| `staged_exe` | 目标同卷的暂存文件 |
| `backup_exe` | 旧 EXE 的备份路径 |

## 回滚

- 重启（`hub` 模式）不需要回滚：没有文件被改。
- 换 EXE 失败在预检阶段：什么都没动。
- 换 EXE 在请求/助手阶段失败：CLI 清理暂存文件、helper 和 plan，旧 EXE 不变。
- helper 已经替换但新 Hub 起不来：用 `<target_exe>.bak` 换回旧 EXE，再照常启动桌面版。CLI 的最终 JSON 里 `exe_replaced: false` 时也会提示备份位置。
- 数据库只在 Hub 停止期间才可能需要回滚；那会丢掉备份之后的记录，先问 owner。

## 排查

- `GET /api/hub/restart-status` 停在 `stopping`：旧 Hub 没有安静退出，看 `hub.log` 末尾有没有 Traceback；桌面壳在等 Hub 进程结束。
- 停在 `resuming` 超过 30 秒：某个会话恢复卡住；`resume_on_start.result.json` 里有每个会话的答案，`failed_count > 0` 时按会话用 `resume_subagent` 单独恢复。
- 托盘「重启 Hub」报「已在关闭或重启中」：上一次重启还没结束，等它完成或看日志，不要强杀进程再点。
- 换 EXE 报 `desktop\dist`：从构建目录直接运行的那份 EXE 不升级，先部署到正式位置。

## Host / Origin 校验

Hub 只服务本机页面和本机客户端：`Host` 必须是 `127.0.0.1`、`localhost` 或 `[::1]`（端口任意，且只允许一个），带 `Origin`
的请求必须来自 Hub 自己的页面（同协议、同端口，且只允许一个非空值），否则 HTTP 返回 403、WebSocket 在握手中被拒。没有
`Origin` 的请求（MCP 客户端、桌面端、`wait_url`）不受影响。在 Hub 前面套代理时，启动前设置
`HUB_ALLOWED_HOSTS=hub.example`、`HUB_ALLOWED_ORIGINS=https://hub.example` 之后再启动；主机条目写成
`host[:port]` 均可（端口被忽略），空条目或写错的条目会在启动时打一条警告后被丢弃，不支持 `*`；两个变量都只在第一个
请求前读取一次。被拒的请求在 `hub.log` 里每个 `(Host, Origin)` 组合每分钟最多一行、全局每分钟最多 20 行，超出的部分在
下一个时间窗用一行 `N refusals not logged` 汇总；行内只有规范化后的主机/来源和经过 `%r` 转义的路径，没有查询串。

## Hub 不再接受连接时

Windows 上，客户端在连接被接受前断开（WinError 64），曾经会让 asyncio 关闭 Hub 的监听端口，而 Hub 进程照常运行：已经打开的连接还能用，新连接全被拒绝。`windows_accept_guard.py` 让 Hub 在接受连接失败后继续监听：连续失败时等待逐步加长（最长 1 秒）后重试，日志只记第一次和此后每第 100 次。

桌面端每 5 秒检查一次自己启动的 Hub：进程还在、但端口连续 3 次没有在监听时，显示错误，不会自动结束 Hub。点「重试」会先停掉旧 Hub 再启动新的，不会出现两个 Hub 同时使用一份数据库。
