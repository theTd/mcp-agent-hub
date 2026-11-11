# Agent Hub 路由提示词审计

日期：2026-11-11

本轮目标是让路由按“先可靠性、后成本”的词典序决策，并明确区分 DSH 任务失败和 DSH 基础设施不可用。提示词按以下顺序维护：目标与优先级、硬性排除规则、主路由表、失败/不可用 fallback、companion 矩阵、输出格式与协议约束。

## 已确认规则

- 同 harness 使用 native subagent；跨 harness 才使用 Agent Hub。
- 高风险困难算法留在当前主代理 root。
- Astra 只有用户明确点名时可用。
- ready、握手和“真实任务稍后提供”返回 defer。
- 分类只看实际动作；Minecraft、GUI、远程等词不能单独触发高档模型。
- DSH 任务失败 → `gpt-6.1-sol xhigh`。
- DSH 不可用 → `gemini-4-argon high`；该 AGY fallback 失败后才 → `gpt-6.1-sol xhigh`。
- Companion 只做并行查缺补漏，不是升级链；普通实现、文档、静态调查、离线分析、GUI 代码和纯截图采集均为 null。

## 边界场景覆盖

压力脚本覆盖以下边界：`terra medium`/`terra high` 的单步与多步、纯截图采集与截图诊断、GUI/Minecraft 背景词、DSH 任务失败与 DSH 不可用、跨系统重构 companion、高风险算法、未点名 Astra、ready 占位，以及 Opus/AGY/Kimi 触发范围。

## 评测协议

每个响应必须输出 `id|primary|companion`。脚本分别统计：

- `primaryErrors`：主路由错误；
- `companionErrors`：companion 错误；
- `missing`：批次中缺失的 id；
- `protocolErrors`：无法解析为三段协议或使用未知 token 的行；
- `inputTokens`、`outputTokens`：服务端 usage 汇总。

一次 primary 错误比 companion 错误更严重；尤其检查不允许的 escalation、提前 fallback 和无条件 Kimi。聚合通过率不能替代分项统计。

## 压测命令

```powershell
$env:ROUTE_TEST_REPEATS="3"
$env:ROUTE_TEST_BATCH="5"
node E:\\codexworktrees\\subagent-launch-args\\mcp-agent-hub\\tools\\route-prompt-stress.mjs
```

若本地评测器未监听 8789，记录外部评测器不可用，并至少运行 `node --check tools/route-prompt-stress.mjs` 与 `git diff --check`。每轮只改变一类规则；合格条件是所有场景可解析、边界无回归、不因背景词升级、不提前调用 fallback、不无条件增加 Kimi、Astra 仍需显式请求，并在可靠性不下降时继续缩短提示词。

## 本轮结果

使用 `ROUTE_TEST_REPEATS=3`、`ROUTE_TEST_BATCH=5` 完成 18 个批次、87 个场景：`87/87` 通过，`primaryErrors=0`，`companionErrors=0`，`missing=0`，`protocolErrors=0`。英文提示词版本的服务端 usage 汇总为输入 21,945 tokens、输出 3,133 tokens。`node --check tools/route-prompt-stress.mjs` 和空白检查均通过。

## Dispatch and waiting 压测

`Dispatch and waiting` 章节从 4,270 字符压缩到 2,412 字符。在相同的 11 个行为场景、3 次重复、每批 11 个输入下，压缩版为 `33/33`，输入 2,856 tokens；基线为 `32/33`，输入 3,918 tokens，减少约 27.1%。两版均无缺行或协议错误；基线唯一偏差是把“中间文本未确认完成”判为 inspect，压缩版保持为 wait。测试脚本为 `tools/dispatch-prompt-stress.mjs`，通过 `DISPATCH_PROMPT_FILE` 指定章节快照。Profile `when_to_use` 已恢复，定位为全局提示词覆盖之外的新路由补充提示。

## 原生 wait handle 提示词压缩（2026-11-12）

针对 `agent_guidance.py` 当前的 Dispatch/waiting 指令重新压缩并使用本机 8789 评测器压测。提示词从 3,459 字符降至 2,585 字符（减少 25.3%）；`tools/dispatch-prompt-stress.mjs` 以 11 个场景、3 次重复、每批 11 个输入运行，压缩前 `29/33`、输入 3,471 tokens，压缩后 `33/33`、输入 2,979 tokens（减少 14.2%）。两版均无缺行或协议错误；旧版把未确认审批和未确认完成误判为 inspect，当前版统一使用 wait_url 等待。
