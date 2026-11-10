# Create a clean Sandbox using a snapshot, so testing never locks dist release files.
[CmdletBinding()]
param([string]$Exe = '')
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $desktop 'dist/mcp-agent-hub-0.1.0-windows-x64-portable.exe' }
$root = Join-Path $desktop ('src-tauri/target/sandbox-' + [guid]::NewGuid().ToString('N'))
$inputDir = Join-Path $root 'input'
$outputDir = Join-Path $root 'output'
New-Item -ItemType Directory -Path $inputDir,$outputDir -Force | Out-Null
Copy-Item -LiteralPath $Exe -Destination (Join-Path $inputDir 'AgentHub.exe')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'sandbox-guest.ps1') -Destination $inputDir
$inputXml = [Security.SecurityElement]::Escape($inputDir)
$outputXml = [Security.SecurityElement]::Escape($outputDir)
$config = @"
<Configuration>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder><HostFolder>$inputXml</HostFolder><SandboxFolder>C:\TestInput</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$outputXml</HostFolder><SandboxFolder>C:\TestOutput</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\TestInput\sandbox-guest.ps1</Command></LogonCommand>
</Configuration>
"@
$wsb = Join-Path $root 'startup.wsb'
[IO.File]::WriteAllText($wsb, $config)
Start-Process -FilePath $wsb
Write-Host "Sandbox test directory: $root"
