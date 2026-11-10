# TUI Agent 的 MCP 可见范围与启动参数

调研日期：2026-11-10。范围来自 `KNOWN_ENGINES`、模型发现器与启动/恢复路径，包含已停用的 Profile。PowerShell 是终端，不是自带 MCP 客户端的 Agent；DSH 虽显示在终端面板里，实际通过原生 API 创建任务。

## 结论

没有一套参数能给所有引擎设置 MCP 白名单。Claude 有独占配置参数；Codex 可逐项禁用已知服务器；其余引擎需要配置文件/目录或上游 API。禁止调用工具不等于从模型工具目录移除服务器。

这里需要区分三个场景：**追加新服务器**（保留原有 MCP）、**限定服务器集合**（排除其他 MCP），以及**修改同名服务器**（可能合并旧配置）。新增能力不代表具备排他加载能力。下表原有列讨论限定集合；新增场景见下一表。

| 引擎 / 本机版本 | 参数能否限定 MCP 服务器 | 做法与限制 |
|---|---|---|
| Claude Code 2.1.322 | 可以，原生独占配置 | `--mcp-config` 加 `--strict-mcp-config`；单用前者不能据此排除其他配置。企业 managed 配置另受官方规则约束。 |
| Codex CLI 0.167.0 | 可以，逐项覆盖 | `-c mcp_servers.<name>.enabled=false`；要知道需排除的名称。插件 MCP 另有配置层；空表覆盖不能清空继承配置。 |
| AGY 1.5.4 | 未找到直接控制加载范围的参数 | `--settings` 与 MCP 文件属于不同配置源；permissions deny 能禁止调用，不能据此认定服务器不可见。 |
| Kimi Code 2.5.2 | 未找到直接控制加载范围的参数 | 现版没有旧 Python CLI 的 `--mcp-config[-file]`。`KIMI_CODE_HOME` 可隔离用户目录，但项目 MCP/插件仍需处理。 |
| OMP 18.18.6 | 间接配置控制，无已确认的单次白名单参数 | `--config` 可关闭项目配置；服务器 denylist 从 active-profile 用户 MCP JSON 读取，普通 YAML overlay 不是 MCP JSON 入口。 |
| DSH（原生 API） | 当前 Hub 接入不适用 CLI 参数 | Hub 的任务 payload 没有 MCP 范围字段；本次没有核查外部 DSH 服务所有能力，不据此推断 DSH 本身不支持。 |
| PowerShell | 不适用 | 可以接收 argv，但终端自身没有 MCP 服务器发现目录。 |

版本来自本机二进制。Codex 特别使用 Profile 指向的 npm CLI 查询，为 0.167.0；桌面应用自带的另一份二进制为 0.167.0-alpha.3.2。在线文档/源码为调研当日上游版本；除下述配置探测外，没有启动模型会话证明最终工具目录。

## 追加新 MCP 的场景

| 引擎 | 不修改共享配置，为新 Sub Agent 追加 MCP | 原有 MCP 与配置寿命 |
|---|---|---|
| Claude | 支持 `--mcp-config=<JSON或文件>`；可直接通过 extra_args 传 JSON，不必先写文件。 | 不带 strict 时继续加载其他 MCP 配置；加 strict 才限定指定集合。配置随本次启动参数生效，Hub 恢复时重传。依据官方参数定义，未做模型会话实测。 |
| Codex | 支持 `-c mcp_servers.<新名称>.url=...` 或 command/args/env；HTTP、stdio 均已做隔离配置解析实测。 | 新名称追加，已有服务器保留；不写用户 config.toml。Hub 恢复时重传；原生 CLI 单独恢复不保证带上这些参数。 |
| AGY | 未确认可直接追加的会话启动参数；已有 `agy mcp add` 或 MCP 配置文件路线。 | add 属于配置管理操作，会改变所选配置源，不能当作仅作用于该子进程的启动参数。 |
| Kimi Code | 无已确认的直接 argv 入口；可预先准备独立 KIMI_CODE_HOME/mcp.json，再由 env 指向该目录。 | 独立用户源会替代默认 Home 的用户配置；要保留原有服务器需显式准备，项目/插件源仍参与加载。文件会保留到清理，不随进程结束自动消失。 |
| OMP | 本机支持 `--plugin-dir <目录>`；上游源码确认该目录中的插件 MCP 会参与发现，是会话追加路线。 | 保留其他发现源，受名称去重、denylist 等规则影响。需要预先准备插件文件；本次做参数/源码核查，未在本机完整运行时验证。普通 --config 不能直接当 MCP JSON 使用。 |
| DSH / PowerShell | DSH 当前 Hub 接口无注入字段；PowerShell 本身不是 MCP 客户端。 | 不应通过向 pwsh 塞参数宣称完成 DSH 会话 MCP 注入。 |

上表的“本次启动”是指 Hub 启动的新 Agent 进程。传参不会给已经运行的 Agent 动态安装 MCP。配置成功也不代表连接、认证或工具发现已成功；后者仍需原生运行时验证。

## Claude Code

[官方 CLI 参考](https://code.claude.com/docs/en/cli-reference)和本机帮助均支持 --mcp-config 加载 JSON 字符串/文件。**只追加一个新 HTTP MCP 并保留其他来源**，可直接传内联 JSON：

```json
{
  "agent_type": "claude",
  "extra_args": [
    "--mcp-config={\"mcpServers\":{\"session_docs\":{\"type\":\"http\",\"url\":\"https://mcp.example.invalid/mcp\"}}}"
  ]
}
```

示例域名需替换为真实服务；extra_args 中的 JSON 是一个完整 token，不额外包 shell 引号。要新增 stdio MCP，将服务器项替换为 `{"command":"node","args":["E:/tools/docs-server.mjs"]}`。这是在 Agent 的配置中定义服务器的启动命令；extra_args 本身是传给 Agent CLI 的参数。

**只加载指定集合**时，可生成所需服务器的 JSON 文件并增加 strict：

```json
{
  "agent_type": "claude",
  "task": "检查当前项目",
  "extra_args": ["--mcp-config=E:/configs/selected-mcp.json", "--strict-mcp-config"]
}
```

`claude` 必须是已启用的实际 Profile ID。等号形式将文件绑定为单个选项值，避免 variadic `--mcp-config <configs...>` 吞掉 Hub 后续追加的初始任务。文件格式为 `{"mcpServers":{"selected":{...}}}`；空集合可用 `{"mcpServers":{}}`。企业边界见[managed MCP 官方说明](https://code.claude.com/docs/en/mcp#exclusive-control-with-managed-mcpjson)。

## Codex

[OpenAI Docs 配置参考](https://developers.openai.com/codex/config-reference/)定义了 `mcp_servers.<id>.enabled`、`enabled_tools` 和 `disabled_tools`；[插件文档](https://developers.openai.com/plugins/build/plugins)另定义 `plugins.<plugin>.mcp_servers.<server>.enabled`。普通 MCP 开关不能自动涵盖插件、产品内置集成或管理员强制配置。

**追加新 HTTP MCP**，无需先写 config.toml：

```json
{
  "agent_type": "codex",
  "extra_args": ["-c", "mcp_servers.session_docs.url=\"https://mcp.example.invalid/mcp\""]
}
```

**追加新 stdio MCP**，可一次传完整 TOML inline table：

```json
{
  "agent_type": "codex",
  "extra_args": [
    "-c",
    "mcp_servers.session_docs={command=\"node\",args=[\"E:/tools/docs-server.mjs\"],env={DOCS_MODE=\"readonly\"}}"
  ]
}
```

url/command/args/env 均为 OpenAI Docs 的原生配置字段。使用不存在的新名称避免与旧服务器字段混合；同名覆盖不保证清掉旧字段，尤其不宜仅设置 url 来把已有 stdio 项切成 HTTP。需要排除旧服务器时，另外逐项禁用：

```json
{
  "agent_type": "codex",
  "extra_args": ["-c", "mcp_servers.unused_a.enabled=false", "-c", "mcp_servers.unused_b.enabled=false"]
}
```

隔离探测：用一次性 CODEX_HOME 配置虚构的 keep/drop stdio 服务器，在空临时工作目录运行 Profile 对应的 `codex mcp list --json`，只读名称和 enabled，不连接 MCP、不调用模型、不改真实配置：

| 参数 | keep | drop |
|---|---|---|
| 无覆盖 | true | true |
| `-c mcp_servers.drop.enabled=false` | true | false |
| `-c mcp_servers={}` | true | true |

追加探测同样使用 Profile 的 0.167.0：传入新 HTTP url 后，列表为 keep/drop/extra_http，三者均 enabled=true；传入新的 stdio inline table 后为 keep/drop/extra_stdio，且 command、包含空格的 args 与 env 均保持原值。没有启动虚构服务器，也没有修改用户配置。

因此空表不是 reset。排他集合需要枚举各配置源中所有未选择的服务器并逐项禁用；只限制某个服务器的工具则使用 enabled_tools/disabled_tools。该探测证明配置合并行为，不是完整模型工具可见性验证。

## AGY

本机 `agy --help` 没有 MCP 白名单/独占配置参数；`agy mcp --help` 只有持久化 add/remove/list/enable/disable 子命令。[设置说明](https://antigravity.google/docs/settings/)与[MCP 文档](https://antigravity.google/docs/mcp?tab=cli)指出服务器来自 `~/.gemini/config/mcp_config.json` 和工作区 `.agents/mcp_config.json`，与 CLI settings.json 分开。

不能把 `--settings '{"mcpServers":{...}}'` 当作服务器筛选。会话级 `permissions.deny`（例如 `mcp(unused/*)`）表达禁止调用。独立 MCP 配置目录/未公开开关尚未验证；不建议改共享文件模拟每个子会话的隔离。

## Kimi Code

[现版命令参考](https://moonshotai.github.io/kimi-code/en/reference/kimi-command)未定义 MCP 配置参数；[配置文档](https://moonshotai.github.io/kimi-code/en/configuration/config-files)支持 KIMI_CODE_HOME。[MCP 文档](https://moonshotai.github.io/kimi-code/en/customization/mcp)说明用户与项目文件合并，同名项目项覆盖用户项，服务器支持 enabled:false，插件也能提供 MCP。

独立 Home 是可继续验证的路线，可用 Hub 已有 env 传入；需要准备登录/模型配置，并处理真实工作目录中的项目 MCP 与插件。单独改 Home 并不证明排除了所有来源。本次不复制凭证、不改变项目配置。

## OMP

本机帮助支持 `--config=<value>`（可重复 config.yml overlay）与 `--profile`，没有直接 MCP 范围参数。[MCP 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md)及[加载源码](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/mcp/config.ts)显示：

- disabledServers 来自 active-profile 用户 MCP JSON，对同名服务器优先禁用；enabledServers 只强制启用同名项，不排除其他名字。
- `mcp.enableProjectConfig:false` 只排除项目级源；其他客户端和插件的用户源仍需处理。
- 多种产品配置参与发现，单独建立空 OMP MCP 文件不足以证明隔离。

可继续验证独立 Profile/agent 目录、显式 denylist 与 discovery 源控制；本次不将其表述为已验证的一次性白名单。普通 --config overlay 不是 MCP JSON 文件入口。

新增 MCP 还可以走插件路线。本机帮助包含 `--plugin-dir`；上游 [helpers.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/discovery/helpers.ts) 的 injectPluginDirRoots 将这些目录加入插件发现，[claude-plugins.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/discovery/claude-plugins.ts) 的 loadMCPServers 读取插件 manifest 的 mcpServers 或根目录 .mcp.json。因此可以准备：

```text
E:/configs/session-mcp-plugin/
  .claude-plugin/plugin.json   # {"name":"session-extra"}
  .mcp.json                   # {"mcpServers":{"docs":{"url":"https://mcp.example.invalid/mcp"}}}
```

然后通过 `extra_args:["--plugin-dir","E:/configs/session-mcp-plugin"]` 加入新子进程。源码会给这类服务器加插件名前缀；它们仍受 discovery 优先级、去重和用户 denylist 影响，不构成白名单。这是基于现版上游源码确认的路线，本机 18.18.6 尚未做完整加载/连接验证。

## DSH

依据本仓库 resolve_launch、_spawn_dsh_hub_job、build_resume_launch：通过 `/_dsh/dsh-crew/jobs` 创建与续接，不启动 Profile 中的 pwsh 参数。筛选 MCP 应先在 DSH 协议定义会话范围，再由 Hub 映射；单加 argv 不起效。

## 本次实现的参数合同

新增 MCP spawn_subagent.extra_args，以及 HTTP `POST /api/sessions`、`POST /api/launch-preview` 的同名字段。控制台「本次启动参数」填写 JSON 字符串数组。

- null/省略/空数组沿用现有行为。非数组、非字符串项、NUL 报错。
- 每项是完整 argv token；空格路径无需额外 shell 引号；支持空字符串/内联 JSON，不做 shell 分词。
- 追加到 Profile args 后，再经过既有模型/思考等级及审批处理，最后由启动模式追加任务和原生会话标识。重复选项如何解析取决于 CLI，不保证最后一个生效。
- 既有识别器命中的模型/思考参数移除并提示；审批仍由 bypass_approvals 控制。普通参数由 CLI 做语义校验。切换 headless、子命令、原生会话身份等开关可能与 Hub TUI 观察不兼容。
- 不改 Profile。原始 extra_args 与最终不含任务的 argv 存入 profile_snapshot；恢复使用该 argv，不再重复追加 extra_args，仍执行已有审批与原生恢复规则。原生不接受的选项仍受各引擎兼容限制。
- MCP 定义若放在外部文件/插件目录中，恢复时该路径仍须存在；Hub 保存 argv，不快照这些外部文件内容。内联 JSON/TOML 则随 argv 一起保存。
- DSH/非 TUI 会话拒绝非空 extra_args，UI 隐藏输入。切换 Profile 或启动成功后清空；不存为浏览器偏好。

上一批交付通用 argv 通道与调研。本次另为 Claude/Codex 增加以下结构化参数；其他引擎仍使用各自的原生途径，不支持时明确报错。

## Codex / Claude 会话级结构化 MCP 配置

`spawn_subagent`、HTTP `POST /api/sessions`、`POST /api/launch-preview` 共用三个可选字段：

| 字段 | 默认 | 作用 |
|---|---|---|
| `mcp_servers` | `{}`（也可省略/null） | 名称到新增服务器配置的对象 |
| `disabled_mcp_servers` | `[]`（也可省略/null） | 按名称禁用；Codex 指普通 MCP 配置名称，不是插件 MCP |
| `strict_mcp_config` | `false` | 仅 Claude 支持；只加载本次指定的集合，企业策略仍适用 |

统一格式只接受 HTTP 的 `url`、可选 `headers`，或 stdio 的 `command`、可选 `args` / `env`。根据 url / command 推断传输类型，二者只能提供一个，不需要 type 字段。名称使用英文字母、数字、下划线、连字符；args 为字符串数组，env / headers 为字符串键值对象。高级传输、OAuth 和插件配置仍使用 extra_args，但不能与非空结构化配置同时重复控制 MCP。

**新增 HTTP MCP，保留其他来源：**

```json
{
  "agent_type": "codex",
  "mcp_servers": {
    "session_docs": {
      "url": "https://mcp.example.invalid/mcp",
      "headers": {"X-Tenant": "demo"}
    }
  }
}
```

替换 agent_type 为已启用的 Claude Profile ID，同一份配置也可使用。HTTP API 将 agent_type 改为 profile_id。示例域名需替换成真实服务。

**新增 stdio MCP：**

```json
{
  "agent_type": "claude",
  "mcp_servers": {
    "session_tools": {
      "command": "node",
      "args": ["E:/tools with spaces/server.mjs"],
      "env": {"MODE": "readonly"}
    }
  }
}
```

**禁用指定名称：**

```json
{
  "agent_type": "claude",
  "disabled_mcp_servers": ["unused"]
}
```

Claude 转为本次 `--settings` 中的 `deniedMcpServers:[{"serverName":"unused"}]`，是阻止加载，不是 permissions.deny 工具规则。名称按原生规则精确匹配；普通用户、项目、插件和 CLI 注入来源均受其限制，进程内 SDK 服务器例外。[官方设置说明](https://code.claude.com/docs/en/settings-reference#deniedmcpservers)。Codex 转为普通 MCP 项的 enabled=false，不覆盖插件 MCP。

**Claude 空独占集合：**

```json
{
  "agent_type": "claude",
  "mcp_servers": {},
  "strict_mcp_config": true
}
```

需要独占但非空时填入 mcp_servers。Codex 对 strict=true 报错，不以空表冒充白名单。Claude 在工作站遇到企业 managed-mcp.json 时可能直接拒绝动态/strict 配置，Hub 不绕过原生策略。[企业策略说明](https://code.claude.com/docs/en/managed-mcp#servers-passed-with-mcp-config-or-strict-mcp-config)。

实现与恢复：

- Codex 将所有新增和禁用项放入一个 `-c mcp_servers={...}` TOML inline table；headers 映射为 http_headers，新增项 enabled=true。原生配置层会合并继承项。本机 CLI 的 -c 点路径不会解码带引号键名，因此把名称放在 TOML 值中交给真正的 TOML 解析器。这个非空覆盖仍不是 reset；同名服务器继续遵循原生合并规则，不保证移除旧传输字段。
- Claude 使用一个 `--mcp-config=<内联JSON>`。普通 --settings 文件/JSON、MCP denylist 和 Hub 审批配置深度合并成单个内联 --settings；对象递归合并、数组保留唯一项，Hub hook 和 ask 规则保留。普通 settings 文件相对本次工作目录解析，合并后的内容随会话保存；恢复不再依赖该外部文件。
- Profile / extra_args 中已有 MCP 原生覆盖时，与非空结构化配置报冲突；Claude settings 内的 MCP 控制项也报冲突。普通 settings 可以合并，无法读取则在启动前报错。字段类型错误、未知字段、NUL、新增与禁用同名均报错。
- 最终 argv 和结构化输入存入现有 profile_snapshot。恢复只复用原生 MCP 参数，并从保存的普通 settings / denylist 重新合并当前 Hub 审批策略，不重复追加 MCP 参数。没有结构化配置的旧会话沿用原有行为。
- 控制台仅为 Codex/Claude TUI 显示折叠配置区；预览列出新增/禁用名称和模式。切换 Profile 或启动成功清空，不存浏览器偏好。配置预览不代表连接或认证成功。

原生隔离验证使用 Codex Profile CLI 0.167.0 和 Claude Code 2.1.322，一次性 CODEX_HOME / CLAUDE_CONFIG_DIR、本地假 HTTP/stdio MCP，不提交模型任务：

- Codex app-server 的 mcpServerStatus/list 实际发现新增 HTTP / stdio 的 probe_marker 工具，保留继承 keep；drop 未收到连接，HTTP header 保持原值。
- Claude SDK 控制协议 initialize / mcp_status 验证新增 HTTP / stdio 均 connected 并请求 tools/list；非 strict 保留 keep，strict 排除 keep，drop 在两种模式均未收到连接；空 strict 集合无服务器连接。stdio 环境变量同时验证保持原值。
- 这证明原生配置加载、连接与工具发现；没有验证 OAuth 或完整模型会话中的最终工具调用。

## 验证

test_session_mcp_config、test_session_launch_args 与 test_session_approvals 合计 31 项通过。mock PTY 测试覆盖 MCP/HTTP 接口一致、校验/冲突、JSON/TOML 转义、结构化配置持久化与恢复、Claude settings 内容快照和审批切换，及原有 5 种 CLI 参数恢复回归。前端通过语法检查，在隔离静态站点配合 mock API 验证 MCP 区域显示、预览名称与模式、非法输入阻止启动、Claude 空 strict、DSH 切换清空、提交 payload 正确、成功后清空与收起、不存浏览器偏好。假会话的 PTY WebSocket 因静态测试服务器无该端点而返回预期 404。原生 CLI 另按上节连接本地假 MCP，未提交模型任务。
