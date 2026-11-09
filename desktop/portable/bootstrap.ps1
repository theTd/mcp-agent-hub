# Runs in a kill-on-close job owned by the desktop. No global Python or PATH changes.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ResourceDir,
    [Parameter(Mandatory=$true)][string]$RuntimeRoot,
    [Parameter(Mandatory=$true)][string]$ProgressPath,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [string]$PythonOverride = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# A desktop launched from PowerShell 7 can inherit its module search path. Always use the
# bundled Windows PowerShell modules, including on PCs whose PATH has no Python or shell entries.
foreach ($module in @('Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility')) {
    Import-Module (Join-Path $PSHOME ("Modules/" + $module + "/" + $module + '.psd1')) -ErrorAction Stop
}
$utf8 = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$lockFile = Join-Path $ResourceDir "requirements.lock"
$lockHash = (Get-FileHash -LiteralPath $lockFile -Algorithm SHA256).Hash
$runtimeSpec = Get-Content -LiteralPath (Join-Path $ResourceDir 'runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Remove-Item -LiteralPath $ResultPath -ErrorAction SilentlyContinue
$statePath = Join-Path $RuntimeRoot "environment.json"
$sequence = 0

function Write-Json($path, $value) {
    $pending = $path + ".pending"
    [IO.File]::WriteAllText($pending, ($value | ConvertTo-Json -Depth 6 -Compress), $utf8)
    Move-Item -LiteralPath $pending -Destination $path -Force
}
function Report($message, $detail = "") {
    Write-Json $ProgressPath @{message=$message;detail=$detail}
    Write-Host "$message $detail"
}
function Hash-Text([string]$text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($text)))).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Quote-Argument([string]$value) {
    # Windows CommandLineToArgvW quoting, including embedded quotes and trailing backslashes.
    return '"' + [regex]::Replace([regex]::Replace($value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}
function Run-Tool([string]$exe, [string[]]$arguments, [int]$timeout = 60) {
    $script:sequence++
    $stdout = Join-Path $RuntimeRoot ("tool-" + $script:sequence + ".out")
    $stderr = Join-Path $RuntimeRoot ("tool-" + $script:sequence + ".err")
    # Start-Process is used rather than shell text: arguments never become executable PowerShell.
    $quoted = @($arguments | ForEach-Object { Quote-Argument $_ }) -join " "
    $proc = Start-Process -FilePath $exe -ArgumentList $quoted -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $null = $proc.Handle
    if (-not $proc.WaitForExit($timeout * 1000)) {
        Stop-Process -Id $proc.Id -ErrorAction SilentlyContinue
        throw "Process timed out: $exe"
    }
    $proc.WaitForExit()
    $text = [IO.File]::ReadAllText($stdout, $utf8)
    $errors = [IO.File]::ReadAllText($stderr, $utf8)
    Remove-Item -LiteralPath $stdout,$stderr -Force
    return @{code=$proc.ExitCode;text=$text;errors=$errors}
}
function Probe([string]$exe) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $null }
    if ($exe -match '\\Microsoft\\WindowsApps\\') { return $null }
    try {
        $r = Run-Tool $exe @('-I','-c', 'import sys,struct,json,venv,ensurepip; print(json.dumps({"implementation":sys.implementation.name,"major":sys.version_info.major,"minor":sys.version_info.minor,"bits":struct.calcsize("P")*8,"source":sys._base_executable,"version":sys.version}))') 15
        if ($r.code -ne 0) { Write-Host "Python probe failed ($($r.code)): $($r.errors)"; return $null }
        $p = $r.text.Trim() | ConvertFrom-Json
        if ($p.implementation -ne 'cpython' -or $p.major -ne 3 -or $p.minor -notin @($runtimeSpec.python_minors) -or $p.bits -ne 64) { return $null }
        if (-not (Test-Path -LiteralPath $p.source -PathType Leaf)) { return $null }
        return $p
    } catch { Write-Host "Rejected Python candidate: $exe"; return $null }
}
function Source-Stamp([string]$exe) {
    $item = Get-Item -LiteralPath $exe
    return "$($item.Length):$($item.LastWriteTimeUtc.Ticks)"
}
function Check-Imports([string]$python) {
    $r = Run-Tool $python @('-I','-c','import fastapi,fastmcp,httpx,pyte,uvicorn,ssl,sqlite3; from PIL import Image; from winpty import PtyProcess') 60
    if ($r.code -ne 0) { Write-Host $r.errors }
    return ($r.code -eq 0)
}
function Complete($state) {
    Write-Json $statePath $state
    Write-Json $ResultPath @{python=$state.python}
    Report 'Python environment ready' $state.python
}

# These changes are restricted to the bootstrap process and its isolated children.
Remove-Item Env:PYTHONHOME,Env:PYTHONPATH,Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PIP_CONFIG_FILE = "NUL"
$env:PIP_CACHE_DIR = Join-Path $RuntimeRoot "pip-cache"
Remove-Item Env:PIP_TARGET,Env:PIP_PREFIX,Env:PIP_USER,Env:PIP_REQUIRE_VIRTUALENV -ErrorAction SilentlyContinue

try {
    Report 'Checking Python environment'
    $cached = $null
    if (Test-Path -LiteralPath $statePath) {
        try {
            $cached = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $overrideProbe = if ($PythonOverride) { Probe $PythonOverride } else { $null }
            if ($cached.lock -eq $lockHash -and (Test-Path -LiteralPath $cached.source) -and
                $cached.stamp -eq (Source-Stamp $cached.source) -and
                (-not $PythonOverride -or ($overrideProbe -and $overrideProbe.source -eq $cached.source)) -and
                (Test-Path -LiteralPath $cached.python) -and (Check-Imports $cached.python)) {
                Complete $cached
                exit 0
            }
        } catch { Write-Host 'Cached environment needs repair'; $cached = $null }
    }

    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($RuntimeRoot))
    if ($drive.AvailableFreeSpace -lt 512MB) { throw 'At least 512 MB of free disk space is required' }
    $candidates = New-Object 'Collections.Generic.List[string]'
    if ($PythonOverride) {
        $candidates.Add($PythonOverride)
    } else {
        if ($cached -and (Test-Path -LiteralPath $cached.source)) { $candidates.Add($cached.source) }
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($py) {
            try {
                $listed = Run-Tool $py.Source @('-0p') 15
                foreach ($line in ($listed.text + "`n" + $listed.errors) -split "`n") {
                    if ($line -match '([A-Za-z]:\\.*?python(?:3[0-9]*)?\.exe)\s*$') { $candidates.Add($matches[1]) }
                }
            } catch { Write-Host 'Python Launcher discovery failed' }
        }
        foreach ($name in @('python.exe','python3.exe')) {
            foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) { $candidates.Add($command.Source) }
        }
        foreach ($hive in @('HKCU:\Software\Python','HKLM:\Software\Python','HKLM:\Software\WOW6432Node\Python')) {
            foreach ($key in @(Get-ChildItem -LiteralPath $hive -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -eq 'InstallPath' })) {
                $exe = $key.GetValue('ExecutablePath')
                if (-not $exe) { $exe = Join-Path ([string]$key.GetValue('')) 'python.exe' }
                $candidates.Add($exe)
            }
        }
        $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
        if ($uv) {
            foreach ($request in @('>=3.11','3.12','3.13','3.14','3.15')) {
                try {
                    $found = Run-Tool $uv.Source @('python','find',$request,'--no-python-downloads','--no-project','--no-config','--offline') 15
                    if ($found.code -eq 0) { $candidates.Add($found.text.Trim()) }
                } catch { Write-Host 'uv local discovery failed' }
            }
        }
    }

    $wheels = Join-Path $RuntimeRoot "wheels"
    New-Item -ItemType Directory -Path $wheels -Force | Out-Null
    function Prepare-Environment($probe) {
        $stamp = Source-Stamp $probe.source
        $key = Hash-Text "$lockHash|$($probe.source)|$stamp|$($probe.version)"
        # Native DLL loading still has MAX_PATH limits on some Windows installations.
        $envDir = Join-Path $RuntimeRoot ("venvs/" + $key.Substring(0,16))
        $python = Join-Path $envDir 'Scripts/python.exe'
        Report 'Creating isolated Hub environment' ($probe.version -split "`n")[0]
        $r = Run-Tool $probe.source @('-I','-m','venv',$envDir) 120
        if ($r.code -ne 0) { Write-Host $r.errors; return $false }
        Report 'Downloading Hub dependencies' 'Python itself is not downloaded when a compatible local interpreter exists'
        $r = Run-Tool $python @('-I','-m','pip','download','--disable-pip-version-check','--timeout','30','--retries','1','--only-binary=:all:','--require-hashes','--dest',$wheels,'-r',$lockFile) 900
        if ($r.code -ne 0) {
            $errorText = $r.text + $r.errors
            # Connection failures can also end in "No matching distribution"; never mistake them for incompatibility.
            if ($errorText -match 'Retrying|SSLError|ProxyError|ConnectionError|ReadTimeout|Could not fetch URL|Failed to establish|timed out|HASHES|hashes') {
                throw "Dependency download failed; Python will not be downloaded as a workaround.`n$errorText"
            }
            # These minor versions are supported by the release's locked wheel set. An index
            # returning no distributions can be a mirror/network problem, not proof of an absent Python.
            throw "Dependency preparation failed.`n$errorText"
        }
        Report 'Installing Hub dependencies'
        $r = Run-Tool $python @('-I','-m','pip','install','--disable-pip-version-check','--no-index','--find-links',$wheels,'--only-binary=:all:','--require-hashes','-r',$lockFile) 300
        if ($r.code -ne 0) { throw "Dependency installation failed.`n$($r.errors)" }
        if (-not (Check-Imports $python)) { throw 'Hub dependency import check failed' }
        $r = Run-Tool $python @('-I','-m','pip','check') 60
        if ($r.code -ne 0) { throw "Dependency consistency check failed: $($r.text)" }
        Complete @{lock=$lockHash;python=$python;source=$probe.source;stamp=$stamp;version=$probe.version}
        return $true
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        Report 'Checking local Python' $candidate
        $probe = Probe $candidate
        if ($probe -and (Prepare-Environment $probe)) { exit 0 }
    }
    if ($PythonOverride) { throw 'The configured Python is not compatible. Correct or remove the python override and retry.' }

    $manifest = Get-Content -LiteralPath (Join-Path $ResourceDir 'runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.url -notmatch '^https://' -or $manifest.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid runtime download manifest' }
    $runtimeDir = Join-Path $RuntimeRoot ("python/" + $manifest.version + "-" + $manifest.sha256.Substring(0,12))
    $managed = Join-Path $runtimeDir 'python/python.exe'
    $probe = Probe $managed
    if (-not $probe) {
        $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($RuntimeRoot))
        if ($drive.AvailableFreeSpace -lt 512MB) { throw 'At least 512 MB of free disk space is required' }
        $downloads = Join-Path $RuntimeRoot 'downloads'
        New-Item -ItemType Directory -Path $downloads -Force | Out-Null
        $archive = Join-Path $downloads ($manifest.sha256 + '.tar.gz')
        if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive).Hash.ToLowerInvariant() -ne $manifest.sha256) {
            Report 'Downloading Python runtime' $manifest.version
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $request = [Net.HttpWebRequest]::Create($manifest.url)
            $request.Timeout = 30000
            $request.ReadWriteTimeout = 30000
            if ($env:HTTPS_PROXY) { $request.Proxy = New-Object Net.WebProxy($env:HTTPS_PROXY) }
            $response = $request.GetResponse()
            $stream = $response.GetResponseStream()
            $output = [IO.File]::Create($archive + '.part')
            try {
                $buffer = New-Object byte[] 65536
                $total = 0L
                $lastPercent = -1
                while (($count = $stream.Read($buffer,0,$buffer.Length)) -gt 0) {
                    $output.Write($buffer,0,$count); $total += $count
                    $percent = if ($response.ContentLength -gt 0) { [int]($total * 100 / $response.ContentLength) } else { -1 }
                    if ($percent -ne $lastPercent) { Report 'Downloading Python runtime' "$percent%"; $lastPercent=$percent }
                }
            } finally { $output.Dispose(); $stream.Dispose(); $response.Dispose() }
            if ((Get-FileHash -LiteralPath ($archive + '.part')).Hash.ToLowerInvariant() -ne $manifest.sha256) { throw 'Python download SHA-256 verification failed' }
            Move-Item -LiteralPath ($archive + '.part') -Destination $archive -Force
        }
        Report 'Extracting verified Python runtime'
        $stage = Join-Path $RuntimeRoot ('python/staging-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        $tar = Join-Path $env:SystemRoot 'System32/tar.exe'
        $r = Run-Tool $tar @('-xzf',$archive,'-C',$stage) 120
        if ($r.code -ne 0 -or -not (Probe (Join-Path $stage 'python/python.exe'))) { throw "Python extraction failed: $($r.errors)" }
        if (Test-Path -LiteralPath $runtimeDir) {
            # Preserve a broken managed runtime for diagnosis instead of deleting unrelated files.
            Move-Item -LiteralPath $runtimeDir -Destination ($runtimeDir + '.broken-' + [guid]::NewGuid().ToString('N'))
        }
        Move-Item -LiteralPath $stage -Destination $runtimeDir
        $probe = Probe $managed
    }
    if (-not $probe -or -not (Prepare-Environment $probe)) { throw 'Downloaded Python cannot run the locked Hub dependencies' }
} catch {
    Write-Host $_.Exception.ToString()
    Report 'Environment preparation failed' $_.Exception.Message
    exit 1
}
