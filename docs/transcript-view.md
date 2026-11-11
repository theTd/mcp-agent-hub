# Transcript 视图

总控台对 Agent 会话默认显示 Hub 生成的结构化 Transcript。它不解析某一个 Agent 的屏幕布局，而是消费 Hub 已经统一接收的 worker 事件、工具调用和中间消息，因此 Codex、Claude、Kimi、OMP、AGY、DSH 等会话使用同一套阅读方式。视图按时间列出任务、进展、工具调用/结果、提问或审批、最终回复和生命周期事件，并每秒刷新当前可见会话。

标题栏的 `TUI` 按钮仍可切换到原始终端画面。TUI 适合检查 ANSI、光标、输入框和 Agent 自己的布局；Transcript 适合快速阅读任务进展。切换只改变显示方式，不会改变会话输入或后台进程。DSH 不提供 TUI 切换；如果 DSH Host 地址可用，标题栏提供 `DSH` 按钮打开其原生界面。

## DeepSeek Harness（DSH）

DSH 通过 DSH Host 的 job、message 和 log 接口运行任务。总控台直接把 Hub 收到的工具调用、工具结果、进度消息、提问、最终回复和生命周期记录展示为 Transcript，不再把 DSH 硬包装成 TUI。需要查看 DSH Host 自己的页面时使用 `DSH` 按钮；没有可用 Host 页面时仍显示 Transcript。连接结果以具体事件和工具结果为准。

## API

`GET /api/sessions/{session_id}/transcript?limit=80` 返回轻量结构化数据。该接口不调用 CLI 专用摘要器，适合高频刷新；`/transcript-summary` 仍用于需要完整、引擎相关摘要的调用方。
