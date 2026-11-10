# Runs only inside the disposable test VM. No host profile or runtime is shared.
$ErrorActionPreference = 'Continue'
$target = Join-Path $env:USERPROFILE 'Desktop\中文 测试'
New-Item -ItemType Directory -Path $target -Force | Out-Null
$exe = Join-Path $target 'Agent Hub.exe'
Copy-Item C:\TestInput\AgentHub.exe $exe
@{python=@(Get-Command python.exe,py.exe,uv.exe -ErrorAction SilentlyContinue).Count;webview=@(Get-ChildItem 'C:\Program Files (x86)\Microsoft\EdgeWebView\Application' -ErrorAction SilentlyContinue).Name;started=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content C:\TestOutput\machine.json -Encoding UTF8
# Deliberately poison installed-edition inputs: portable must ignore them.
$oldDir = Join-Path $env:APPDATA 'mcp-agent-hub-desktop'
New-Item -ItemType Directory -Path $oldDir -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $oldDir 'config.json'),'{"repo_dir":"E:\\old-hub","python":"E:\\missing.exe"}')
$env:AGENT_HUB_REPO = 'E:\old-hub'
$env:AGENT_HUB_PYTHON = 'E:\missing.exe'
$env:AGENT_HUB_PORT = 'invalid'
Start-Process -FilePath $exe
$deadline = (Get-Date).AddMinutes(20)
while ((Get-Date) -lt $deadline) {
    $logs = Join-Path $env:LOCALAPPDATA 'mcp-agent-hub-portable\logs'
    if (Test-Path $logs) { Copy-Item "$logs\*" C:\TestOutput -Force -ErrorAction SilentlyContinue }
    $runtime = Join-Path $env:LOCALAPPDATA 'mcp-agent-hub-portable\portable'
    foreach ($file in @('progress.json','prepared.json','environment.json','launch.json')) { Copy-Item (Join-Path $runtime $file) C:\TestOutput -Force -ErrorAction SilentlyContinue }
    if (Test-Path (Join-Path $runtime 'launch.json')) {
        $launch = Get-Content (Join-Path $runtime 'launch.json') -Raw | ConvertFrom-Json
        & curl.exe --noproxy '*' --max-time 2 --silent --fail "http://127.0.0.1:$($launch.port)/api/hub/info" --output C:\TestOutput\hub-info.json
    }
    Start-Sleep -Seconds 3
}
