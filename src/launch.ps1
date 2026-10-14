$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectDir
$RuntimeDir = Join-Path $ProjectDir 'runtime'
$VenvDir = Join-Path $RuntimeDir '.venv'
$PythonExe = Join-Path $VenvDir 'Scripts\python.exe'
$InstallLog = Join-Path $RuntimeDir 'install.log'
$ServerOutLog = Join-Path $RuntimeDir 'server.stdout.log'
$ServerErrorLog = Join-Path $RuntimeDir 'server.stderr.log'
$DependencyStamp = Join-Path $RuntimeDir 'dependencies.sha256'
$Requirements = Join-Path $ProjectDir 'config\requirements.txt'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:WXMOMENTS_RUNTIME_DIR = $RuntimeDir
$env:WECHAT_TOOL_ENABLE_CONSOLE_LOG = '0'

function Invoke-Native([scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    & $Command
    $script:RuntimeExitCode = $LASTEXITCODE
}

function Show-StartupError([string]$Message, [string[]]$LogPaths) {
    $Details = @($Message)
    foreach ($LogPath in $LogPaths) {
        if (Test-Path -LiteralPath $LogPath) {
            $Details += [IO.Path]::GetFileName($LogPath)
            $Details += Get-Content -LiteralPath $LogPath -Tail 100
        }
    }
    $SafeMessage = [Net.WebUtility]::HtmlEncode($Message)
    $SafeDetails = [Net.WebUtility]::HtmlEncode(($Details -join "`n"))
    $ErrorPage = Join-Path $RuntimeDir 'startup-error.html'
    $Page = @"
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>wxMoments - 启动提示</title><style>
:root{color-scheme:light dark}body{font:15px/1.7 'Microsoft YaHei',sans-serif;margin:40px auto;padding:0 20px;max-width:900px}
h1{font-size:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #8886;padding:16px;border-radius:12px}
a{color:#07a854}</style><h1>wxMoments 启动未完成</h1><p>$SafeMessage</p>
<p>解决下方问题后，重新双击 run.bat。详细记录保存在项目的 runtime 目录。</p>
<details open><summary>启动记录</summary><pre>$SafeDetails</pre></details></html>
"@
    [IO.File]::WriteAllText($ErrorPage, $Page, [Text.UTF8Encoding]::new($false))
    if (-not $env:WXMOMENTS_NO_BROWSER) {
        Start-Process -FilePath $ErrorPage
    }
}

function Test-ServiceUp {
    try {
        $Client = [Net.Sockets.TcpClient]::new()
        try {
            $Client.Connect('127.0.0.1', 8756)
            return $Client.Connected
        } finally {
            $Client.Dispose()
        }
    } catch {
        return $false
    }
}

function Stop-PortListener([int]$Port) {
    $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $Listener) { return }
    Stop-Process -Id $Listener.OwningProcess -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw '旧服务未能停止，请手动结束占用 8756 端口的进程后重试。'
    }
}

try {
    [IO.Directory]::CreateDirectory($RuntimeDir) | Out-Null
    # 每次双击都停掉旧服务再启动：保证代码改动一定生效。
    Stop-PortListener 8756
    $ConfigPath = Join-Path $ProjectDir 'config\config.json'
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Copy-Item -LiteralPath (Join-Path $ProjectDir 'config\config.example.json') -Destination $ConfigPath
    }
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($PythonLauncher) {
            Invoke-Native { & $PythonLauncher.Source -3 -m venv $VenvDir *> $InstallLog }
        } else {
            $PythonLauncher = Get-Command python -ErrorAction SilentlyContinue
            if (-not $PythonLauncher) { throw '未找到 Python，请安装 Python 3.10 或更新版本。' }
            Invoke-Native { & $PythonLauncher.Source -m venv $VenvDir *> $InstallLog }
        }
        if ($RuntimeExitCode -ne 0 -or -not (Test-Path -LiteralPath $PythonExe)) {
            throw '无法创建运行环境，请查看安装记录。'
        }
    }
    $Fingerprint = Invoke-Native { & $PythonExe -c 'import hashlib, sys; from pathlib import Path; print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest().upper() + chr(58) + sys.version)' $Requirements 2>> $InstallLog }
    if ($RuntimeExitCode -ne 0) { throw 'Python 运行环境损坏，请查看安装记录。' }
    $SavedFingerprint = if (Test-Path -LiteralPath $DependencyStamp) { Get-Content -LiteralPath $DependencyStamp -Raw } else { '' }
    if ($SavedFingerprint.Trim() -ne $Fingerprint) {
        Invoke-Native { & $PythonExe -m ensurepip --upgrade *> $InstallLog }
        if ($RuntimeExitCode -ne 0) { throw '无法准备依赖安装工具。' }
        $PipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $Requirements)
        $WheelsDir = Join-Path $ProjectDir 'wheels'
        if (Test-Path -LiteralPath $WheelsDir) {
            Invoke-Native { & $PythonExe @PipArgs --no-index --find-links $WheelsDir *>> $InstallLog }
            if ($RuntimeExitCode -ne 0) { Invoke-Native { & $PythonExe @PipArgs --find-links $WheelsDir *>> $InstallLog } }
        } else {
            Invoke-Native { & $PythonExe @PipArgs *>> $InstallLog }
        }
        if ($RuntimeExitCode -ne 0) { throw '依赖安装失败，请检查网络并查看安装记录。' }
        Invoke-Native { & $PythonExe -m pip check *>> $InstallLog }
        if ($RuntimeExitCode -ne 0) { throw '依赖检查失败，请查看安装记录。' }
        [IO.File]::WriteAllText($DependencyStamp, $Fingerprint)
    }
    # 独立进程启动服务，不随本窗口退出而结束；窗口只负责调度与验证。
    $ServerProc = Start-Process -FilePath $PythonExe -ArgumentList (Join-Path $PSScriptRoot 'server.py') `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $ServerOutLog -RedirectStandardError $ServerErrorLog
    $Up = $false
    for ($i = 0; $i -lt 120; $i++) {
        if (Test-ServiceUp) { $Up = $true; break }
        if ($ServerProc.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $Up) {
        throw '本地服务启动失败，请查看运行记录。'
    }
    # 浏览器由 server.py 在服务就绪后自动打开，这里只提示地址。
    Write-Host 'Open in browser: http://127.0.0.1:8756/'
    exit 0
} catch {
    Show-StartupError $_.Exception.Message @($InstallLog, $ServerOutLog, $ServerErrorLog)
    exit 1
}
