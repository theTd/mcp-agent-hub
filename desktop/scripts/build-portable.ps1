# Build a single Windows x64 EXE with Hub resources, but no Python distribution or installer.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
foreach ($module in @('Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility')) {
    Import-Module (Join-Path $PSHOME ("Modules/" + $module + "/" + $module + '.psd1')) -ErrorAction Stop
}

$desktopDir = Split-Path -Parent $PSScriptRoot
$target = "x86_64-pc-windows-msvc"
$previousTargetDir = $env:CARGO_TARGET_DIR
$previousBuildJobs = $env:CARGO_BUILD_JOBS
$previousRustFlags = $env:CARGO_ENCODED_RUSTFLAGS
$previousReleaseDebug = $env:CARGO_PROFILE_RELEASE_DEBUG
$stagingDir = $null
$pendingZip = $null

Push-Location -LiteralPath $desktopDir
try {
    if ($env:OS -ne "Windows_NT") {
        throw "The portable release must be built on Windows."
    }

    $config = Get-Content -LiteralPath "src-tauri/tauri.conf.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$config.version
    if ($version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$') {
        throw "Expected a literal semantic version in tauri.conf.json."
    }

    # Keep the output deterministic even when the caller has a different Cargo target directory.
    $env:CARGO_TARGET_DIR = Join-Path $desktopDir "src-tauri/target"
    # Release linking can be expensive; default to a single Cargo job on this workstation.
    if (-not $env:CARGO_BUILD_JOBS) { $env:CARGO_BUILD_JOBS = "1" }
    & node.exe scripts/check-portable-privacy.mjs
    if ($LASTEXITCODE -ne 0) { throw 'Source privacy gate failed. No release was published.' }
    $repoDir = Split-Path -Parent $desktopDir
    $sysroot = (& rustc --print sysroot).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate compiler sysroot.' }
    $cargoRoot = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
    $flags = @('-Cdebuginfo=0','-Clink-arg=/PDBALTPATH:%_PDB%',
        "--remap-path-prefix=$repoDir=/src", "--remap-path-prefix=$cargoRoot=/cargo",
        "--remap-path-prefix=$sysroot=/toolchain", "--remap-path-prefix=$env:USERPROFILE=/build-user")
    $env:CARGO_ENCODED_RUSTFLAGS = $flags -join [char]31
    $env:CARGO_PROFILE_RELEASE_DEBUG = '0'
    & pnpm.cmd exec tauri build --no-bundle --target $target --features portable --config src-tauri/tauri.portable.conf.json -- --locked
    if ($LASTEXITCODE -ne 0) { throw "Tauri build failed (exit $LASTEXITCODE)." }

    $exe = Join-Path $env:CARGO_TARGET_DIR "$target/release/mcp-agent-hub-desktop.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "The build did not produce $exe."
    }

    $outputDir = Join-Path $desktopDir "dist"
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    $stagingDir = Join-Path $outputDir ("portable-staging-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stagingDir | Out-Null
    $privacyReport = Join-Path $stagingDir 'privacy.json'
    & node.exe scripts/check-portable-privacy.mjs --exe $exe --report $privacyReport
    if ($LASTEXITCODE -ne 0) { throw 'EXE privacy gate failed. No release was published.' }
    $archiveName = "mcp-agent-hub-$version-windows-x64-portable.zip"
    $archivePath = Join-Path $outputDir $archiveName
    $pendingZip = Join-Path $outputDir ([guid]::NewGuid().ToString("N") + ".zip")

    # Explicit allowlist: no repository, venv, config.json, database, logs, or credentials.
    Copy-Item -LiteralPath $exe -Destination (Join-Path $stagingDir "MCP Agent Hub.exe")
    Copy-Item -LiteralPath "portable/README.zh-CN.md" -Destination $stagingDir
    Copy-Item -LiteralPath "portable/config.example.json" -Destination $stagingDir
    $payload = @("MCP Agent Hub.exe", "README.zh-CN.md", "config.example.json")
    $payloadPaths = @($payload | ForEach-Object { Join-Path $stagingDir $_ })
    Compress-Archive -LiteralPath $payloadPaths -DestinationPath $pendingZip -CompressionLevel Optimal
    Move-Item -LiteralPath $pendingZip -Destination $archivePath -Force
    $singleExe = Join-Path $outputDir "mcp-agent-hub-$version-windows-x64-portable.exe"
    Copy-Item -LiteralPath $exe -Destination $singleExe -Force
    Copy-Item -LiteralPath $privacyReport -Destination ($singleExe + '.privacy.json') -Force
    $hash = (Get-FileHash -LiteralPath $singleExe -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText(($singleExe + '.sha256'), ($hash + '  ' + (Split-Path -Leaf $singleExe) + "`n"))
    Write-Host "Portable EXE: $singleExe"
    Write-Host "Optional ZIP with documentation: $archivePath"
}
finally {
    # Only remove the unique staging directory created above, after checking its absolute parent.
    if ($stagingDir -and (Test-Path -LiteralPath $stagingDir)) {
        $resolvedStaging = [IO.Path]::GetFullPath($stagingDir)
        $expectedParent = [IO.Path]::GetFullPath((Join-Path $desktopDir "dist"))
        if ((Split-Path -Parent $resolvedStaging) -eq $expectedParent -and
            (Split-Path -Leaf $resolvedStaging) -match '^portable-staging-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
        }
    }
    if ($pendingZip -and (Test-Path -LiteralPath $pendingZip)) {
        Remove-Item -LiteralPath $pendingZip -Force
    }
    $env:CARGO_TARGET_DIR = $previousTargetDir
    $env:CARGO_BUILD_JOBS = $previousBuildJobs
    $env:CARGO_ENCODED_RUSTFLAGS = $previousRustFlags
    $env:CARGO_PROFILE_RELEASE_DEBUG = $previousReleaseDebug
    Pop-Location
}
