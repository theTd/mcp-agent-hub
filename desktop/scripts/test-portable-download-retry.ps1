# Interrupt only this test's downloader, then verify a clean retry of its partial archive.
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$root = Join-Path $desktop ('src-tauri/target/download-retry-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$runner = Join-Path $root 'runner.ps1'
$bootstrap = Join-Path $root 'throttled-bootstrap.ps1'
# Slow only the test copy's transfer so a fast local proxy cannot hide the partial-file window.
$source = [IO.File]::ReadAllText((Join-Path $desktop 'portable/bootstrap.ps1'))
$source = $source.Replace('$output.Write($buffer,0,$count);', 'Start-Sleep -Milliseconds 20; $output.Write($buffer,0,$count);')
[IO.File]::WriteAllText($bootstrap, $source)
[IO.File]::WriteAllText($runner, @'
param($Bootstrap,$ResourceDir,$RuntimeRoot)
function Get-Command { [CmdletBinding()] param([string]$Name,[switch]$All) }
function Get-ChildItem { [CmdletBinding()] param([string]$LiteralPath,[switch]$Recurse) }
& $Bootstrap -ResourceDir $ResourceDir -RuntimeRoot $RuntimeRoot -ProgressPath (Join-Path $RuntimeRoot 'progress.json') -ResultPath (Join-Path $RuntimeRoot 'prepared.json')
exit $LASTEXITCODE
'@)
$argsList = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$runner,'-Bootstrap',$bootstrap,'-ResourceDir',(Join-Path $desktop 'portable'),'-RuntimeRoot',$root)
$quoted = @($argsList | ForEach-Object { '"' + $_ + '"' }) -join ' '
$process = Start-Process powershell -ArgumentList $quoted -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'interrupted.out') -RedirectStandardError (Join-Path $root 'interrupted.err')
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        # Windows can report Length=0 until an open FileStream is closed.
        $partial = @(Get-ChildItem (Join-Path $root 'downloads') -Filter '*.part' -ErrorAction SilentlyContinue)
        if ($partial.Count -gt 0) { break }
        if ($process.HasExited -or [DateTime]::UtcNow -gt $deadline) { throw "Download did not begin; see $root" }
        Start-Sleep -Milliseconds 100
    } while ($true)
    Stop-Process -Id $process.Id
    $process.WaitForExit()
    if (Test-Path (Join-Path $root 'prepared.json')) { throw 'Interrupted download published readiness' }
    Write-Host 'PASS: interrupted transfer leaves a partial archive without readiness'
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id }
}
& powershell @argsList *> (Join-Path $root 'retry.log')
if ($LASTEXITCODE -ne 0) { throw "Download retry failed; see $root" }
$manifest = Get-Content (Join-Path $desktop 'portable/runtime.json') -Raw | ConvertFrom-Json
$archive = Join-Path $root ('downloads/' + $manifest.sha256 + '.tar.gz')
if ((Get-FileHash $archive).Hash.ToLowerInvariant() -ne $manifest.sha256 -or -not (Test-Path (Join-Path $root 'prepared.json'))) { throw 'Retry did not publish a verified environment' }
Write-Host 'PASS: retry replaces partial transfer with a verified runtime and ready environment'
Write-Host "Artifacts: $root"
