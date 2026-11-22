# 构建桌面版

## Windows 便携版 EXE

便携版使用 Tauri/Rust 构建，入口脚本在 `desktop/scripts/build-portable.ps1`。在 Windows PowerShell 中执行：

```powershell
cd desktop
pnpm install --frozen-lockfile
pnpm build:portable
```

便携版构建还会编译 Rust 终端核心（缓存在 `terminal-core/target`，与 `desktop/src-tauri/target` 分开），并把
`hub_terminal_core.pyd` 放到 `desktop/portable/` 再打进 EXE；核心与 EXE 使用同样的路径重映射，因为只设
`strip = "symbols"` 时 `.pyd` 里仍会留下 Cargo registry 路径。

需要 Node.js、pnpm、Rust MSVC 工具链和 Windows WebView2 开发环境。首次构建会编译 Rust 依赖，后续构建会使用 `desktop/src-tauri/target` 的增量缓存。

构建要在正常的交互式 Windows 桌面会话（`WinSta0\Default`）里运行。离屏或无头会话里，`tauri-build` 生成的 `build-script-build` 可能以 `0xc0000142 (STATUS_DLL_INIT_FAILED)` 失败：它静态链接了 `shell32.dll`、`ole32.dll`，在那种会话里无法完成 DLL 初始化。

默认构建使用一个 Cargo 任务以控制资源占用；机器资源充足时可以提高并行度：

```powershell
$env:CARGO_BUILD_JOBS = "16"
pnpm build:portable
```

成品写入 `desktop/dist`：

- `mcp-agent-hub-<version>-windows-x64-portable.exe`
- 同名 `.sha256` 校验文件
- 同名 `.privacy.json` 隐私门禁报告
- 同名 `.zip`（EXE、中文说明和示例配置）

构建会先扫描源码资源，完成后再扫描 EXE 中的构建路径、用户名、机器名、凭据和 PE 调试记录。任一检查失败都不会发布成品。

## 普通安装版

仍可在 `desktop` 目录执行：

```powershell
pnpm install --frozen-lockfile
pnpm build
```

这条命令生成原有 Tauri/NSIS 安装版；需要单文件便携 EXE 时使用 `pnpm build:portable`。
