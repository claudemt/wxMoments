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
    # Windows PowerShell treats native stderr as ErrorRecord; retain it in the
    # log and decide success from the exit code rather than from stderr output.
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
    Start-Process -FilePath $ErrorPage
}

try {
    [IO.Directory]::CreateDirectory($RuntimeDir) | Out-Null
    try {
        $Status = Invoke-RestMethod -Uri 'http://127.0.0.1:8756/api/moments/status' -TimeoutSec 2
        if ($Status.ok -eq $true -and $null -ne $Status.ready) {
            Start-Process 'http://127.0.0.1:8756/'
            exit 0
        }
    } catch { }
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
    Invoke-Native { & $PythonExe (Join-Path $PSScriptRoot 'server.py') 1> $ServerOutLog 2> $ServerErrorLog }
    if ($RuntimeExitCode -ne 0) { throw '本地服务启动或运行失败，请查看运行记录。' }
    exit 0
} catch {
    Show-StartupError $_.Exception.Message @($InstallLog, $ServerOutLog, $ServerErrorLog)
    exit 1
}
