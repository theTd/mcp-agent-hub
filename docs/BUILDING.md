# 构建桌面版

## Windows 便携版 EXE

便携版使用 Tauri/Rust 构建，入口脚本在 `desktop/scripts/build-portable.ps1`。在 Windows PowerShell 中执行：

```powershell
cd desktop
pnpm install --frozen-lockfile
pnpm build:portable
```

需要 Node.js、pnpm、Rust MSVC 工具链和 Windows WebView2 开发环境。首次构建会编译 Rust 依赖，后续构建会使用 `desktop/src-tauri/target` 的增量缓存。

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
