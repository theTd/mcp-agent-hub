# MCP Agent Hub 单 EXE 便携启动版

双击 `mcp-agent-hub-<版本>-windows-x64-portable.exe` 即可启动。EXE 内置 Hub 后端、前端和资源，不依赖源码仓库，不内置 Python 安装包，无需管理员权限。

## 首次启动

1. 检查本机 Python，优先使用显式配置的解释器、有效缓存，以及 Python Launcher、PATH、注册表和已安装 uv 能找到的本地解释器。探测 uv 时禁止自动下载。
2. 验证 CPython、x64 和创建隔离环境的能力；当前锁定依赖支持 Python 3.11～3.14，更新的版本需待依赖兼容验证后加入支持清单。
3. 使用兼容的本机 Python 创建 Hub 专用环境；不向系统 Python 安装包，不修改系统 PATH。
4. 仅当没有兼容的本机解释器时，下载固定的 CPython 3.12.13 独立运行时，校验 SHA-256 后展开到 AppData，不执行系统安装。
5. 安装经过哈希校验的锁定依赖后启动 Hub。后续启动复用环境；准备完成后可离线启动桌面和本地 Hub，AI 服务仍可能需要网络。

首次下载失败时，启动页显示原因，可以修复网络或配置后点击“重试”。网络错误不会触发额外的 Python 下载。已有有效缓存会保留；没有完整缓存时不能保证离线首次启动。

## 运行要求

- Windows x64，系统已有 Microsoft Edge WebView2 Runtime。本包不携带 WebView2。
- 首次准备可能访问 PyPI 和 GitHub 上 Astral python-build-standalone 的固定发布文件。支持标准代理环境变量；不会跳过 TLS 或哈希验证。
- AI CLI 仍需自行安装和登录，EXE 不携带 CLI、凭据或个人会话。

## 配置与数据

默认不需要填写仓库或 Python 路径。EXE 可以随意移动，以下数据留在当前电脑：

- 桌面配置：`%APPDATA%\mcp-agent-hub-portable\config.json`。
- 日志：`%LOCALAPPDATA%\mcp-agent-hub-portable\logs`，包括 `desktop.log`、`hub.log`、`bootstrap.log`。
- 内嵌程序资源、隔离 Python 环境、下载缓存：`%LOCALAPPDATA%\mcp-agent-hub-portable\portable`。
- 便携版数据库、运行配置：上述 `portable\data` 目录。
- Hub profiles：上述 `portable\data\agent_profiles.json`，不读取安装版的 profiles。
- AI CLI 登录和历史：由各 CLI 按原有方式管理。

默认优先使用 `4444`，占用时依次选择 `4445`～`4544`；不会连接该端口已有的其他 Hub。独立配置只支持可选的 `python`、`port` 字段，例如 `{"port":4500}`。显式指定的端口被占用时直接报错。`config.example.json` 的空对象表示自动选择端口和解释器。

便携版始终使用内嵌 Hub。旧 `repo_dir`、`AGENT_HUB_REPO`、`AGENT_HUB_PYTHON`、`AGENT_HUB_PORT` 不影响便携版；不读取或迁移安装版配置、数据库和 profiles。

明确指定的 Python 不可用时会报错，请修正或删除 `python` 覆盖后重试。解释器被卸载、依赖变化或缓存损坏时会重新准备环境。程序不会自动迁移旧仓库中的会话数据库。

## 桌面行为与排查

安装版和便携版使用独立的单实例身份、通知和开机启动注册，可同时运行。重复打开便携版只激活便携版窗口。关闭窗口会隐藏到托盘，退出请使用托盘菜单。移动 EXE 后需要重新设置已启用的开机启动。

启动失败时先看错误页和 `bootstrap.log`。下载或安装失败请检查网络、代理和磁盘余量；端口被其他程序占用时修改端口。缺少 WebView2 时请安装微软的系统 WebView2 Runtime。

可运行 `EXE --portable-prepare-only` 只准备、检查环境而不打开窗口或启动 Hub；成功退出码为 0，结果记录在 `portable\prepared.json`、`portable\launch.json`，故障记录在日志中。该诊断方式不会停止已有 Hub。

## 从源码构建

在 `desktop` 目录运行 `pnpm install --frozen-lockfile`，再运行 `pnpm build:portable`。
输出单 EXE 到 `desktop/dist`，同时提供带说明和示例配置的可选 ZIP。打包不下载或携带 Python；首次运行按上述规则准备环境。默认单个 Cargo 编译任务，可通过 `CARGO_BUILD_JOBS` 调整。

完整的构建前置条件、并行编译设置、产物清单和普通安装版命令见仓库 [`docs/BUILDING.md`](../../docs/BUILDING.md)。

构建使用逐文件资源清单和路径重映射；发布前扫描原始资源及 EXE 的 ASCII、UTF-8、UTF-16 和 PE 调试记录，拒绝构建机路径、用户名、机器名及凭据。扫描未通过不更新发布文件。成品旁提供 `.privacy.json` 和 `.sha256` 检查记录。

`pnpm build` 继续生成原有 NSIS 安装版。源码后端未指定 `HUB_DATA_DIR` 时仍使用原数据位置；该变量可将数据库和运行配置放到独立目录。
