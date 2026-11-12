# Hub 重启与会话恢复

Hub 停止时仍在运行的会话，在下次启动时标为「已中断」并归档，可以在「归档」里恢复（同一个会话 id）。终端类 Agent（Codex、Claude、Kimi 等）随 Hub 一起结束，恢复时用各自的原生记录重新打开；DSH 任务在 DSH Host 里继续运行，恢复时重新接上同一个任务（见 [记录视图](transcript-view.md) 的 DSH 一节）。

## 有意重启时一并恢复（resume_on_start.json）

主 Agent 可能一直在等 Hub 回来：如果 Hub 先开端口、再由人逐个恢复，主 Agent 会先看到子 Agent「已中断」（等待接口回答 `failed`），可能当成失败处理，或与人工恢复同时调用恢复。为避免这一点，重启前在 Hub 的数据目录（便携版为 `%LOCALAPPDATA%\mcp-agent-hub-portable\portable\data`）放一个 `resume_on_start.json`：

```json
{"session_ids": ["codex-…"], "continue_unfinished_turns": true, "continue_session_ids": ["codex-…"], "continue_text": "…"}
```

各字段都可省略：不写 `session_ids` 表示「停止时仍在运行的全部会话」；`continue_unfinished_turns` 默认开启；`continue_text` 有默认文字。`session_ids` 里列出的会话如果已经被更早的一次启动归档为「已中断」（之后没有恢复过），也会恢复；这时已不知道它停止时的状态，所以只有列在 `continue_session_ids` 里的会话才附带继续消息。写了 `continue_session_ids` 时，附带消息只看这个列表。

下次启动时 Hub：

1. 取走这个文件（改名为 `resume_on_start.taken.json`，只用一次），记下停止时仍在运行的会话和它们当时的状态，再照常归档。
2. 端口打开后逐个恢复这些会话：先 DSH（只是重新接上任务），后终端类 Agent。终端类 Agent 启动时会连 Hub 自己的 MCP 端点，所以要等端口打开后再启动它们。
3. 恢复期间，除了 `/health`、`/api/hub/info`、`/api/hub/shutdown`，所有请求都先等着，最多 30 秒，恢复完立即放行。所以主 Agent 看到的会话已经在运行，它发起的恢复也不会和启动时的恢复抢着做。桌面端检查 Hub 是否就绪不受影响。
4. 停止时一个回合还没做完的终端类 Agent（状态为运行中、等主 Agent 回复或启动中），恢复时附带一条消息：说明 Hub 重启打断了上一回合、正在运行的命令或审批提示都已停止，请它从中断处继续，原来在等的审批要重新申请。这条消息在记录视图里以「你」显示。空闲的会话和 DSH 会话不带消息。
5. 把每个会话的结果写到 `resume_on_start.result.json`（`ok`、`continued`、`status_at_stop`，失败时有 `error`）。恢复不了的会话照常留在「归档」里，原因写在记录上。

## 重启线上 Hub：给 Agent 的操作步骤

只在 owner 明确要求时做。流程：写恢复请求 → 结束桌面 App → 备份 → 换 EXE → 用 explorer 启动 → 核对。依赖没变时停机约 30 秒；`desktop/portable/requirements.lock` 变了时，首次启动要新建 Python 环境，约 2–3 分钟。

**停止用强制结束，不用托盘「退出」。** 托盘「退出」和「重启 Hub」是正常停止：结束每个终端 Agent，**取消正在运行的 DSH 任务**，把所有会话归档为「已中断」。之后这些会话已不算「停止时仍在运行」，`resume_on_start.json` 默认一个也不恢复。强制结束桌面 App 时，它的 Job 对象一并结束 Hub 和终端 Agent；DSH 任务在 DSH Host 里继续跑，数据库里这些会话仍是运行中。这正是恢复请求设计和测试的情形（`test_restart_resume_on_start.py` 的 `stopped_hard`）。代价是后台写队列里最后几条未提交的记录可能丢失（DECISIONS.md 的 buffered-durability）。停止令牌只在 App 和 Hub 进程的内存里，不要去找它。

命令在 PowerShell 7 里运行（Claude Code 的 PowerShell 工具就是），工作目录为仓库根目录。每个代码块自成一体：工具调用之间变量不保留。

### 1. 准备

- 构建新 EXE：`cd desktop; pnpm build:portable`，产物是 `desktop\dist\mcp-agent-hub-0.1.0-windows-x64-portable.exe`。
- 不要从 `desktop\dist` 直接运行它（WebView2 0x800700AA 崩溃）。也不要在线上 Hub 运行时试启动任何一份 EXE：4444 被占时，它会在 4445 用同一个数据目录再起一个 Hub，把所有会话归档为「已中断」，恢复请求就落空了。
- 看一眼现状，记下哪些会话在运行：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -like "mcp-agent-hub*" } | Select-Object ProcessId, ExecutablePath
curl.exe -s --noproxy "*" http://127.0.0.1:4444/api/hub/info
(curl.exe -s --noproxy "*" http://127.0.0.1:4444/api/sessions | ConvertFrom-Json).sessions | Select-Object session_id, status, title
```

### 2. 停止、备份、换 EXE、启动（一个代码块，一次运行）

先填 `$B`：仓库外的备份目录，遵守你的临时文件规则。代码块在动手前做检查，任何一项不满足就 `throw` 并且什么都不改；这时停下来问 owner。

```powershell
$B   = "<备份目录>"
$new = Join-Path (git rev-parse --show-toplevel) "desktop\dist\mcp-agent-hub-0.1.0-windows-x64-portable.exe"

$ErrorActionPreference = "Stop"
$D   = Join-Path $env:LOCALAPPDATA "mcp-agent-hub-portable\portable\data"
$app = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like "mcp-agent-hub*" })
if ($app.Count -ne 1) { throw "expected one desktop app, found $($app.Count)" }
$exe = $app[0].ExecutablePath   # owner 平时运行的位置，新 EXE 放这里
if ($exe -like "*\desktop\dist\*") { throw "the app runs from desktop\dist; ask the owner" }
if (-not (Test-Path $new)) { throw "no new build at $new" }
if (Test-Path "$D\resume_on_start.json") { throw "a resume request is already pending; ask the owner" }
New-Item -ItemType Directory -Force $B | Out-Null
Set-Content "$B\exe-path.txt" $exe   # 回滚时用

# 恢复请求：停止时仍在运行的会话全部恢复，被打断回合的终端 Agent 收到继续消息
Set-Content -Encoding ascii "$D\resume_on_start.json" '{"continue_unfinished_turns": true}'

# 强制结束桌面 App；Job 对象带走 Hub 和终端 Agent
$hubIds = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*mcp-agent-hub-portable*server.py*" }).ProcessId
Stop-Process -Id $app[0].ProcessId -Force
Start-Sleep -Seconds 3
if ($hubIds) { Get-Process -Id $hubIds -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Seconds 2 }
if (Get-NetTCPConnection -LocalPort 4444 -State Listen -ErrorAction SilentlyContinue) { throw "port 4444 still listening" }

# 备份（Hub 已停，文件静止），再换 EXE
Copy-Item "$D\hub_state.db*", "$D\wait_inbox.db", $exe -Destination $B
Copy-Item $new $exe -Force
if ((Get-FileHash $new).Hash -ne (Get-FileHash $exe).Hash) { throw "EXE copy mismatch" }

# 用 explorer 启动，App 不继承 Agent 的环境变量
explorer.exe $exe
"started $exe sha256 $((Get-FileHash $exe).Hash)"
```

停机期间，你自己和其他父 Agent 的 agent-hub MCP 连接会断开，Hub 回来后自动重连；恢复进行时，除 `/health` 和桌面端探测外的请求都先等着（最多 30 秒）。

### 3. 等恢复完成

每次最多等 160 秒；`done=False` 就再运行一次。新建 Python 环境时 `progress.json` 依次显示 Creating isolated Hub environment、Downloading Hub dependencies、Python environment ready。

```powershell
$P = Join-Path $env:LOCALAPPDATA "mcp-agent-hub-portable"; $D = "$P\portable\data"
$end = (Get-Date).AddSeconds(160)
do {
  $done = -not (Test-Path "$D\resume_on_start.json") -and (Test-Path "$D\resume_on_start.result.json") -and
          (Get-Item "$D\resume_on_start.result.json").LastWriteTime -ge (Get-Item "$D\resume_on_start.taken.json").LastWriteTime
  if (-not $done) { Start-Sleep -Seconds 3 }
} until ($done -or (Get-Date) -gt $end)
"done=$done"
Get-Content "$P\portable\progress.json" -Raw
Select-String -Path "$P\logs\desktop.log" -Pattern "hub ready on port|error view|port 4445" | Select-Object -Last 2 | ForEach-Object Line
```

### 4. 核对

```powershell
$P = Join-Path $env:LOCALAPPDATA "mcp-agent-hub-portable"; $D = "$P\portable\data"
(Get-Content "$D\resume_on_start.result.json" -Raw | ConvertFrom-Json).sessions.PSObject.Properties |
  ForEach-Object { "{0} ok={1} continued={2} at_stop={3} {4}" -f $_.Name, $_.Value.ok, $_.Value.continued, $_.Value.status_at_stop, $_.Value.error }
curl.exe -s --noproxy "*" http://127.0.0.1:4444/api/hub/info
(curl.exe -s --noproxy "*" http://127.0.0.1:4444/api/sessions | ConvertFrom-Json).sessions | Select-Object session_id, status, current_activity
$log = "$P\logs\hub.log"
$n = (Select-String -Path $log -Pattern "desktop shell: starting hub" | Select-Object -Last 1).LineNumber
Get-Content $log | Select-Object -Skip ($n - 1) | Select-String "Traceback|ERROR"
```

成功的样子：
- 第 1 步里在运行的会话都在 result 里，`ok=True`；当时回合没做完的终端 Agent `continued=True`。
- `/api/hub/info` 的 `started_at` 是刚才，`repo` 指向新的 `portable\resources\<hash>`。要确认改动已部署，就在那个目录里搜一个这次改动的函数名。
- 会话列表里它们在运行，DSH 会话的步数在增加。
- 这次启动之后的 hub.log 没有 Traceback 或 ERROR。

出现下面任何一种情况，就停下来把日志末尾报告给 owner，不要反复重启，也不要再启动一份 EXE：
- desktop.log 有 `error view`、`port 4445`；
- 5 分钟后还没有 `hub ready on port 4444`（另看 `logs\bootstrap.log`）；
- result 里有 `ok=False`。这样的会话留在「归档」里，原因写在记录上，可以用 `resume_subagent` 单独恢复。

### 5. 回滚

新构建起不来或有问题、owner 同意回滚时，把第 2 步备份的旧 EXE 换回去。不要为回滚改第 2 步的代码块：它的备份行会用新 EXE 覆盖 `$B` 里的旧 EXE。

```powershell
$B = "<第 2 步用的备份目录>"

$ErrorActionPreference = "Stop"
$D   = Join-Path $env:LOCALAPPDATA "mcp-agent-hub-portable\portable\data"
$exe = (Get-Content "$B\exe-path.txt").Trim()
$old = Join-Path $B (Split-Path $exe -Leaf)
if (-not (Test-Path $old)) { throw "no backup EXE at $old" }
$app = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like "mcp-agent-hub*" })
if ($app.Count -gt 1) { throw "more than one desktop app; ask the owner" }
if (-not (Test-Path "$D\resume_on_start.json")) { Set-Content -Encoding ascii "$D\resume_on_start.json" '{"continue_unfinished_turns": true}' }
$hubIds = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*mcp-agent-hub-portable*server.py*" }).ProcessId
if ($app) { Stop-Process -Id $app[0].ProcessId -Force; Start-Sleep -Seconds 3 }
if ($hubIds) { Get-Process -Id $hubIds -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Seconds 2 }
if (Get-NetTCPConnection -LocalPort 4444 -State Listen -ErrorAction SilentlyContinue) { throw "port 4444 still listening" }
Copy-Item $old $exe -Force
explorer.exe $exe
"rolled back $exe sha256 $((Get-FileHash $exe).Hash)"
```

然后照第 3、4 步核对。只有数据库损坏时，才在 Hub 停止期间把 `$B` 里的 `hub_state.db*` 和 `wait_inbox.db` 拷回数据目录：那会丢掉备份之后的所有记录，先问 owner。

### 6. 收尾

- **旧 Python 环境**：依赖变了时，`portable\venvs\` 下会多出一个目录。当前用的是 desktop.log 最后一条 `hub launched` 行里的那个；其余的不再使用。确认没有进程从它运行（`Get-CimInstance Win32_Process | Where-Object ExecutablePath -like "*venvs\<旧 hash>*"`），征得 owner 同意后按字面路径删除。
- **备份目录 `$B`**：留到 owner 确认新 Hub 正常，然后删除。
- `resume_on_start.taken.json` 和 `resume_on_start.result.json` 留在数据目录里，下次重启时会被覆盖。

## Hub 不再接受连接时

Windows 上，客户端在连接被接受前断开（WinError 64），曾经会让 asyncio 关闭 Hub 的监听端口，而 Hub 进程照常运行：已经打开的连接还能用，新连接全被拒绝。`windows_accept_guard.py` 让 Hub 在接受连接失败后继续监听：连续失败时等待逐步加长（最长 1 秒）后重试，日志只记第一次和此后每第 100 次。

桌面端每 5 秒检查一次自己启动的 Hub：进程还在、但端口连续 3 次没有在监听时，显示错误，不会自动结束 Hub。点「重试」会先停掉旧 Hub 再启动新的，不会出现两个 Hub 同时使用一份数据库。
