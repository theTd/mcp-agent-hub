# Targeted regression scenarios. Requires a compatible local Python and package-index access.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Python, [switch]$DownloadTests, [string]$ReuseRoot)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$bootstrap = Join-Path $desktop 'portable/bootstrap.ps1'
$resources = Join-Path $desktop 'portable'
$root = Join-Path $desktop ('src-tauri/target/bootstrap-tests-' + [guid]::NewGuid().ToString('N'))
if ($ReuseRoot) { $root = [IO.Path]::GetFullPath($ReuseRoot) }
New-Item -ItemType Directory -Path $root -Force | Out-Null
$utf8 = New-Object Text.UTF8Encoding($false)
function Assert($condition, $message) { if (-not $condition) { throw $message }; Write-Host "PASS: $message" }
function Run-Bootstrap($name, $override, [string]$resourceOverride = $resources, [switch]$NoDiscovery, [switch]$LowDisk) {
    # Windows PowerShell converts native stderr to ErrorRecord even with output redirection.
    $ErrorActionPreference = 'Continue'
    $runtime = Join-Path $root $name
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $arguments = @('-ResourceDir',$resourceOverride,'-RuntimeRoot',$runtime,'-ProgressPath',(Join-Path $runtime 'progress.json'),'-ResultPath',(Join-Path $runtime 'prepared.json'))
    if ($override) { $arguments += @('-PythonOverride',$override) }
    if ($LowDisk) {
        # Exercise the real failure branch with a deterministic disk threshold in a test copy.
        $lowDiskBootstrap = Join-Path $runtime 'low-disk-bootstrap.ps1'
        [IO.File]::WriteAllText($lowDiskBootstrap, ([IO.File]::ReadAllText($bootstrap).Replace('.AvailableFreeSpace -lt 512MB', '.AvailableFreeSpace -ge 0')), $utf8)
        & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $lowDiskBootstrap @arguments *> (Join-Path $runtime 'test.log')
    } elseif ($NoDiscovery) {
        # Pester-style command seams simulate a PC with no Python, without changing this PC.
        $runner = Join-Path $runtime 'no-discovery.ps1'
        [IO.File]::WriteAllText($runner, @'
param($Bootstrap,$ResourceDir,$RuntimeRoot,$ProgressPath,$ResultPath)
function Get-Command { [CmdletBinding()] param([string]$Name,[switch]$All) }
function Get-ChildItem { [CmdletBinding()] param([string]$LiteralPath,[switch]$Recurse) }
& $Bootstrap -ResourceDir $ResourceDir -RuntimeRoot $RuntimeRoot -ProgressPath $ProgressPath -ResultPath $ResultPath
exit $LASTEXITCODE
'@, $utf8)
        & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -Bootstrap $bootstrap @arguments *> (Join-Path $runtime 'test.log')
    } else {
        & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $bootstrap @arguments *> (Join-Path $runtime 'test.log')
    }
    return $LASTEXITCODE
}

$code = Run-Bootstrap 'local' $Python
Assert ($code -eq 0) 'local Python creates isolated environment'
$localRoot = Join-Path $root 'local'
Assert (-not (Test-Path -LiteralPath (Join-Path $localRoot 'downloads'))) 'local Python causes zero Python downloads'
$savedIndex = $env:PIP_INDEX_URL
$savedRetries = $env:PIP_RETRIES
Remove-Item -LiteralPath (Join-Path $root 'network-failure/environment.json') -ErrorAction SilentlyContinue
try {
    $env:PIP_INDEX_URL = 'https://127.0.0.1:1/simple'
    $env:PIP_RETRIES = '0'
    Assert ((Run-Bootstrap 'local' $Python) -eq 0) 'prepared environment starts with index unavailable'
    Assert ((Run-Bootstrap 'network-failure' $Python) -ne 0) 'dependency network failure is reported'
    Assert (-not (Test-Path (Join-Path $root 'network-failure/downloads'))) 'network failure does not trigger Python download'
} finally { $env:PIP_INDEX_URL = $savedIndex; $env:PIP_RETRIES = $savedRetries }
Assert ((Run-Bootstrap 'network-failure' $Python) -eq 0) 'retry after network repair succeeds'
Assert ((Run-Bootstrap 'bad-override' (Join-Path $root 'missing-python.exe')) -ne 0) 'missing explicit interpreter is rejected'
Assert ((Run-Bootstrap 'low-disk' '' -NoDiscovery -LowDisk) -ne 0) 'insufficient disk space blocks environment creation'
Assert ((Get-Content (Join-Path $root 'low-disk/test.log') -Raw) -match '512 MB') 'disk-space failure gives an actionable reason'
Assert (-not (Test-Path (Join-Path $root 'low-disk/downloads'))) 'disk-space failure never begins a Python download'
$state = Get-Content (Join-Path $localRoot 'environment.json') -Raw | ConvertFrom-Json
$state.lock = 'obsolete-lock'
[IO.File]::WriteAllText((Join-Path $localRoot 'environment.json'),($state | ConvertTo-Json),$utf8)
Assert ((Run-Bootstrap 'local' $Python) -eq 0) 'stale environment metadata is repaired using local Python'

if ($DownloadTests) {
    Assert ((Run-Bootstrap 'download' '' -NoDiscovery) -eq 0) 'no local Python downloads and verifies fixed runtime'
    $manifest = Get-Content (Join-Path $resources 'runtime.json') -Raw | ConvertFrom-Json
    $download = Join-Path $root ('download/downloads/' + $manifest.sha256 + '.tar.gz')
    Assert ((Get-FileHash -LiteralPath $download).Hash.ToLowerInvariant() -eq $manifest.sha256) 'download hash matches pinned manifest'
    $badResources = Join-Path $root 'bad-resources'
    New-Item -ItemType Directory -Path $badResources -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $resources 'requirements.lock') -Destination $badResources
    $manifest.sha256 = '0' * 64
    [IO.File]::WriteAllText((Join-Path $badResources 'runtime.json'),($manifest | ConvertTo-Json),$utf8)
    Assert ((Run-Bootstrap 'bad-hash' '' $badResources -NoDiscovery) -ne 0) 'incorrect runtime hash prevents installation'
    Assert (-not (Test-Path (Join-Path $root 'bad-hash/prepared.json'))) 'failed verification never publishes a ready environment'
}
Write-Host "Test artifacts retained at $root"
