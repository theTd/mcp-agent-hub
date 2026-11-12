# 记录视图（结构化 Transcript）

总控台对 Agent 会话默认显示 Hub 生成的结构化记录。它不解析某一个 Agent 的屏幕布局，而是消费 Hub 已经统一接收的 worker 事件、工具调用和中间消息，因此 Codex、Claude、Kimi、OMP、AGY、DSH 等会话使用同一套阅读方式。可见会话每秒刷新一次；刷新只修补变化的条目，展开的行、滚动位置、选中的文字和正在输入的回复都会保留。

## 显示内容

- **任务**：会话的任务原文（去掉 Hub 附加的桥接说明）。
- **消息**：Agent 的进展消息和向主 Agent 的汇报（「汇报」），以及主 Agent 发来的消息；按 Markdown 子集渲染（标题、列表、表格、代码块、行内代码、粗体、http(s) 链接），所有文本先转义再加标签。
- **工具调用**：调用和结果合成一行，显示工具名、从参数推出的一行标题（命令、文件、描述）、耗时和状态；展开后看输入和输出，可分别复制；每段最多保存 4000 字符，更长的末尾标注「…（已截断，原文 N 字符）」。没有描述参数时标题取命令列表的第一条、`apply_patch` 的文件或前几个参数；长绝对路径缩成 `…\文件名`。
- **思考**：引擎记录下的推理内容，默认折叠（同样最多 4000 字符）。Claude、Kimi、OMP、AGY 和 DSH 的思考从各自的原生记录中单独读出，不混进消息，也不作为中间消息发给主 Agent；Codex 的推理是加密的，没有可显示的内容。Kimi 会把每一步写两遍（边做边写的流式片段，和事后整条的消息），Hub 只读流式片段。旧版本记录里思考和回复连在一条消息中：最终回复前面的部分按思考显示，重复的回复只显示一次。
- **活动组**：两条消息之间连续的工具调用和思考折叠成一组，组头显示数量、用到的工具、失败数和最新一步；最后一组在 Agent 工作时保持展开。
- **提问 / 审批**：已处理的显示成普通行，标题是被审批的命令或问题本身，右侧是回复内容；被自动审批规则放行的也显示一行，标为「已自动批准」；等待回复的高亮显示。worker 自己转发给主 Agent 的审批通知不再重复显示。
- **最终回复**和**生命周期提示**（终止、出错、投递失败等）。

## 交互

- 停在底部时自动跟随最新内容；往上翻后出现「回到最新 / N 条新内容」按钮。切换单窗口/平铺、改变窗口大小时跟随状态不变。
- 默认载入最近 80 条，顶部「载入更早的记录」每次多取 120 条（上限 400），载入后视口停在原处，已展开的活动组保持展开（较早的条目并入第一组时也一样）。
- 有待回复的提问或审批时，底部固定一张回复卡：选项按钮、自定义回复框（Enter 发送）。卡片高度随窗格高度收缩，平铺时也能看到上方记录。
- 刷新请求 10 秒超时；刷新进行中又触发的刷新会排队，在当前请求结束后再执行一次。刷新失败时保留上次的内容，顶部提示错误。
- 标题栏的「记录 / TUI」切换原始终端画面（快捷按键只在 TUI 下显示）。切换只改变显示方式，不会改变会话输入或后台进程。

## DeepSeek Harness（DSH）

DSH 会话通过 DSH Host 的 job / message 接口运行，没有自己的终端，因此只显示记录视图。

工具调用的完整输入、输出和思考来自 DSH 自己的会话记录：`$DSH_HOME/sessions/<项目>/<会话 id>/session.v4.jsonl.zstd`（未设置 `DSH_HOME` 时为 `~/.dsh`）。Hub 只解码已经写完的 zstd 帧，按 `seq` 增量读取；读到的位置存在会话的 `conversation_ref`：`dsh_record_seq`（最后一个事件）和 `dsh_record_offset`（下一帧的字节位置，继续任务时从这里读而不是从头解码；位置无效时从头读并按 seq 过滤）。重启后重新接上的任务若没有保存的位置，会跳过记录里已有的事件，不会重复。解码需要 Python 3.14（`compression.zstd`）或 Node.js ≥ 24（`node:zlib` 的 zstd）。找不到记录或无法解码时自动退回 job 日志，此时只有工具名和进展消息。

记录文件在 job 启动后约 1–2 秒才出现。Hub 在 job 开始后的 20 秒内持续查找，期间暂不读 job 日志，避免同一步骤出现两次；20 秒仍找不到就改用 job 日志。job 结束时再查找一次，然后每 0.5 秒补读，直到没有新帧（最多 6 次），收尾的工具结果和最终消息不会漏掉。读到损坏的帧时停止读记录并退回 job 日志。

DSH 审批卡显示的是给人看的提示：去掉了写给主 Agent 的回复说明，选项变成按钮。

DSH 原生网页界面需要 `dsh web` 打印的访问令牌，Hub 无法代为登录，所以总控台不再嵌入它（Profile 也不再有 GUI 地址字段）。旧的 `web_gui` 类 DSH Profile 在终端里运行 `dsh headless`，保留「记录 / TUI」切换。

## API

`GET /api/sessions/{session_id}/transcript?limit=80`（`limit` 10–400）返回轻量结构化数据，不调用 CLI 专用摘要器，适合高频刷新；`/transcript-summary` 仍用于需要完整、引擎相关摘要的调用方。

| 字段 | 含义 |
| --- | --- |
| `entries` | 时间顺序的条目；`kind` 为 `task`、`assistant`（`channel: "report"` 表示汇报）、`parent`、`thinking`、`tool`、`interaction`、`final`、`notice` |
| `entries[].tool` | `name`、`title`、`input`、`output`、`status`（`running` / `ok` / `error` / `unknown`）、`started_at`、`ended_at` |
| `entries[].interaction` | `request_id`、`kind`、`title`、`summary`、`prompt`、`options`；另有 `pending` 和 `resolution`（`text`、`by`: `parent` / `auto`） |
| `pending_interaction` | 当前等待回复的提问或审批（同上结构，不含引擎元数据） |
| `has_more` / `hidden_count` | 是否还有更早的条目、被截掉的条目数 |
| `status`、`step_count`、`current_activity`、`final_response`、`updated_at` | 会话状态摘要 |

工具调用的结构化细节（输入、输出、调用 id、成败）随事件存入 `worker_events.data_json`，会话归档或 Hub 重启后记录视图仍然完整。
