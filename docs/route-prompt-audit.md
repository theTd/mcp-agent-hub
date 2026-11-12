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

## 工作区与审批提示词（2026-11-11）

起因：一次 DSH 派发把 worker 的 cwd 设为 worktree，测试框架、日志和结果却放在 cwd 之外，于是几乎每次编辑、运行、读日志都要父代理逐个审批；父代理还想用 auto_approval_rules 和“本会话允许”减少审批。核对代码：不只 DSH，hub 注入的审批参数也只覆盖 cwd：Codex 用 `-s workspace-write`，cwd 外写入要审批；Claude 用 `acceptEdits`，只自动接受工作目录内的编辑；DSH worker 只能写 cwd，hub 发给 DSH 的任务只带 `cwd`，无法加根目录；auto_approval_rules 只匹配带结构化 `argv` 的审批，而目前没有任何引擎的审批元数据带 `argv`，规则存下也不会命中；`set_subagent_approval_rules` 却总是返回 `rules_applied: true`。旧提示词反而建议“重复的低风险审批优先用 auto_approval_rules”。

改动（同一类规则）：

- `agent_guidance.py`：删去上述建议，改为三条：对所有引擎，派发前把任务自己创建的 worktree、临时、日志、结果放进 cwd，因为沙箱和自动接受只覆盖 cwd（DSH 在 cwd 外每次调用都要审批，读也一样，且无法加根目录）；只放任务创建的东西，源码、共享或他人的数据、凭据等非任务创建的文件留在原处，也不把 cwd 扩大到父目录、home 或盘符根来覆盖它们；cwd 外的重复审批说明布局错了，先答当前这一个，再只把任务创建的文件移进 cwd，或从包含它们的任务目录重新派发，不要设 auto_approval_rules；DSH 完全访问请求选 Allow once，用户没同意就不选 Allow for this session。
- `server.py`：`spawn_subagent` 写明 cwd 的含义；`set_subagent_approval_rules` 写明规则结构、匹配条件、目前不会命中，以及 `rules_applied` 只表示已保存。
- `session_manager.py`：每条审批请求附带的提示补上同样两点。
- `tools/dispatch-prompt-stress.mjs`：新增 10 个场景：DSH 的 `harness`、`outside`、`logreads`、`grant`、`oneoff`（最后一个防止对单次 cwd 外读取过度触发 layout，接受 respond、once 或 inplace），非 DSH 的 `reports`（Codex 派发前定路径）和 `sibling`（Claude 反复申请编辑自己在 cwd 旁目录创建的临时文件），以及防止过度执行的 `shared`（他人共享数据集）、`creds`（home 下的凭据）、`parentdir`（把含其他项目的父目录设为 cwd）；新增动作 `layout`、`once`、`session`、`rules`、`inplace`、`widen` 并各给一行中性定义。

迭代中修正了两处测试设计：已有动作是主题标签，新动作不加定义时模型把 `rules` 理解为“适用规则主题”；场景 id 原为 `autorules`，模型直接按 id 名答 `rules`，改为 `logreads`。提示词方面，描述句（“cannot cut approvals”“use it only with the user's consent”）在批量模式下几乎无效，改成禁止句后 grant 从 `0/5` 升到 `5/5`。

最终结果（本机 8789 评测器，16 个场景，每批 16 个输入，每轮 5 次重复；基线 2 轮，修订版 3 轮）：

| | 基线 | 修订版 |
|---|---|---|
| 总计 | `140/160` | `236/240` |
| 原 11 个场景 | `110/110` | `164/165` |
| harness → layout | `10/10` | `15/15` |
| outside → layout | `10/10` | `15/15` |
| logreads → layout | `0/10`（全判 rules） | `13/15` |
| grant → once | `0/10`（全判 session） | `15/15` |
| oneoff → respond/once | `10/10` | `14/15` |

所有轮次 `missing=0`、`protocolErrors=0`。提示词从 2,595 字节增至 3,061 字节（+18%），每轮输入从 6,020 增至 6,545 tokens（+8.7%）。残留偏差：修订版 pending 一次判为 wait、oneoff 一次判为 layout、logreads 两次判为 rules；基线在 pending 和 oneoff 上 10 次无偏差，样本较小，后续轮次需继续观察。代码侧仍未修：没有引擎上报 `argv`，`rules_applied` 总为 true。

修订（2026-11-12）：上表时第一条只针对 DSH；按 owner 意见改为适用所有引擎，并加入 `reports`、`sibling` 两个场景（共 18 个，每批 18 个输入，每轮 5 次重复）。原始提示词 1 轮 `77/90`；只针对 DSH 的版本 2 轮 `176/180`；适用所有引擎的版本 2 轮 `179/180`。非 DSH 场景：原始 `8/10`（sibling 两次判为 session），两个修订版均 `20/20`，即在本评测里扩大适用范围没有带来可测的提升，收益在于文字不再误导 Codex/Claude 派发。logreads：只针对 DSH 版 `6/10`，当前版 `9/10`。所有轮次 `missing=0`、`protocolErrors=0`。该版提示词 3,163 字节（比原始 +22%），每轮输入 6,930 tokens（比原始 6,290 +10.2%）。

修订二（2026-11-12）：按 owner 意见防止调用方过度执行“放进 cwd”。加入 `shared`、`creds`、`parentdir` 三个场景和 `inplace`、`widen` 两个动作（共 21 个，每批 21 个输入，每轮 5 次重复），并把 `logreads`、`sibling` 写明是任务自己的文件。上一版暴露出过度执行：`parentdir` `5/5` 判为 widen，`shared` `0/5`（3 次 widen、2 次 move）。按“只放任务创建的东西、其他留原处、不扩大 cwd”改写后：

| | 原始（1 轮） | 上一版（1 轮） | 当前版（2 轮） |
|---|---|---|---|
| 总计 | `58/105`（一批 21 行缺失，下列分母为 4） | `87/105` | `203/210` |
| shared → inplace | `0/4` | `0/5` | `10/10` |
| creds → inplace | `2/4` | `3/5` | `10/10` |
| parentdir → layout | `0/4`（全判 widen） | `0/5`（全判 widen） | `8/10` |
| pending → respond | `4/4` | `3/5` | `10/10` |
| logreads → layout | `0/4`（全判 rules） | `3/5` | `7/10` |

当前版 `missing=0`、`protocolErrors=0`；最弱的是 `logreads`（2 次 inplace、1 次 rules），另有 `approval` 两次判为 inspect。“existing data 留原处”曾让模型把任务自己的日志也留在原处，改为“非任务创建的文件”后 logreads 从 `4/10` 回到 `7/10`。当前提示词 3,387 字节（比原始 +30%），每轮输入 7,920 tokens（比原始 7,030 +12.7%）。

```powershell
$env:DISPATCH_PROMPT_FILE="<SERVER_INSTRUCTIONS 快照>"
$env:DISPATCH_TEST_REPEATS="5"
$env:DISPATCH_TEST_BATCH="21"
node tools/dispatch-prompt-stress.mjs
```

## 默认原生自动审批（2026-11-12，D13）

Owner 要求：引擎有原生自动审批时默认使用，主 Agent 只有刻意传参才换模式。`agent_guidance.py` 把 `bypass_approvals=null inherits profile…` 一行换成 `approval_mode` 规则（不传即 auto，Codex `--approve-for-me`、Claude auto mode；`ask` 只在主 Agent 或用户要亲自批每个高风险操作时用；`bypass` 只在用户要求或任务需要时用；`bypass_approvals` 为旧开关，不得同时传），并把 cwd 一行里的 “Claude acceptEdits” 限定为 ask 模式。

`tools/dispatch-prompt-stress.mjs` 新增 4 个场景与动作 `omit`、`ask`、`bypass`：`modedefault`（普通 Codex 任务，期望 omit）、`modesafe`（想“显式又稳妥”时的反射式覆盖，期望 omit）、`modeself`（用户要亲自批，期望 ask）、`modenone`（用户明确要求不审批，期望 bypass）。

本机 8789 评测器，25 个场景、每批 25 个输入：

| 版本 | 结果 | 偏差 | 输入 tokens/轮 |
|---|---|---|---|
| 基线（master，3 次重复） | `72/75` | `modesafe` 2 次判 ask，`parentdir` 1 次判 inplace | 1,742 |
| 初版规则（3 次重复） | `71/75` | `modesafe` 3/3 判 ask，`parentdir` 1 次判 inplace | 1,849 |
| 定稿规则（5 次重复） | `124/125` | `modesafe` 1 次判 ask | 1,869 |

初版只写“leave approval_mode unset”，`modesafe` 全部被“稳妥”带向 ask；定稿加上 “even to be explicit or careful: the default, auto, is the safe choice” 并把 ask 限定为“你或用户要亲自批每个高风险操作”后升到 `4/5`。基线不含 `approval_mode` 也能答对另三个新场景（场景文本已足够明确），所以它们只防回归。`parentdir` 的偶发 inplace 在基线中同样出现，与本次改动无关。提示词 3,927 字节（基线 3,389，+15.9%）。
