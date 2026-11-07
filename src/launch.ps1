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
$OptionalRequirements = Join-Path $ProjectDir 'config\requirements-optional.txt'
$OptionalMarker = Join-Path $RuntimeDir 'optional-missing.txt'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:WXMOMENTS_RUNTIME_DIR = $RuntimeDir
$env:WECHAT_TOOL_ENABLE_CONSOLE_LOG = '0'

function Invoke-Native([scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    & $Command
    $script:RuntimeExitCode = $LASTEXITCODE
}
function Remove-DuplicateEnvVars {
    try {
        $vars = [Environment]::GetEnvironmentVariables('Process')
        $groups = $vars.GetEnumerator() | Group-Object { $_.Key.ToUpperInvariant() }
        foreach ($group in $groups) {
            if ($group.Count -le 1) { continue }
            $keep = $group.Group | Where-Object { $_.Key -ceq $_.Key.ToUpperInvariant() } | Select-Object -First 1
            if (-not $keep) { $keep = $group.Group[0] }
            foreach ($item in $group.Group) {
                if ($item.Key -cne $keep.Key) {
                    [Environment]::SetEnvironmentVariable($item.Key, $null, 'Process')
                }
            }
            [Environment]::SetEnvironmentVariable($keep.Key, $keep.Value, 'Process')
        }
    } catch {
    }
}

function Show-StartupError([string]$Message, [string[]]$LogPaths) {
    Write-Host ''
    Write-Host "  [FAILED] $Message" -ForegroundColor Red
    Write-Host "  Install log: $InstallLog"
    if (Test-Path -LiteralPath $ServerOutLog) { Write-Host "  Server log:  $ServerOutLog" }
    if (Test-Path -LiteralPath $ServerErrorLog) { Write-Host "  Error log:   $ServerErrorLog" }
    Write-Host ''
}

function Install-OptionalPackages {
    if (-not (Test-Path -LiteralPath $OptionalRequirements)) { return }
    if (Test-Path -LiteralPath $OptionalMarker) { return }
    Invoke-Native { & $PythonExe -c 'import pilk' 2>$null }
    if ($RuntimeExitCode -eq 0) { return }
    Write-Host '  - Installing optional voice codec...'
    Invoke-Native { & $PythonExe -m pip install --disable-pip-version-check --only-binary=:all: -r $OptionalRequirements *>> $InstallLog }
    if ($RuntimeExitCode -ne 0) {
        [IO.File]::WriteAllText($OptionalMarker, 'optional packages unavailable')
        Write-Host '  - Optional voice codec skipped: voice messages stay as .silk files.'
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
        throw "Port $Port is still in use. Close the process that occupies it and run run.bat again."
    }
}

try {
    [IO.Directory]::CreateDirectory($RuntimeDir) | Out-Null
    Write-Host '  - Checking runtime...'
    Remove-DuplicateEnvVars
    Stop-PortListener 8756
    $ConfigPath = Join-Path $ProjectDir 'config\config.json'
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Copy-Item -LiteralPath (Join-Path $ProjectDir 'config\config.example.json') -Destination $ConfigPath
    }
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Host '  - First run: preparing Python environment...'
        $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($PythonLauncher) {
            Invoke-Native { & $PythonLauncher.Source -3 -m venv $VenvDir *> $InstallLog }
        } else {
            $PythonLauncher = Get-Command python -ErrorAction SilentlyContinue
            if (-not $PythonLauncher) { throw 'Python not found. Install Python 3.10+ and enable "Add to PATH", then run run.bat again.' }
            Invoke-Native { & $PythonLauncher.Source -m venv $VenvDir *> $InstallLog }
        }
        if ($RuntimeExitCode -ne 0 -or -not (Test-Path -LiteralPath $PythonExe)) {
            throw 'Could not create the Python environment. Check runtime\install.log.'
        }
    }
    $Fingerprint = Invoke-Native { & $PythonExe -c 'import hashlib, sys; from pathlib import Path; print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest().upper() + chr(58) + sys.version)' $Requirements 2>> $InstallLog }
    if ($RuntimeExitCode -ne 0) { throw 'The Python environment is broken. Delete the runtime folder and run run.bat again.' }
    $SavedFingerprint = if (Test-Path -LiteralPath $DependencyStamp) { Get-Content -LiteralPath $DependencyStamp -Raw } else { '' }
    if ($SavedFingerprint.Trim() -ne $Fingerprint) {
        Write-Host '  - Installing dependencies (1-3 minutes, keep this window open)...'
        Invoke-Native { & $PythonExe -m ensurepip --upgrade *> $InstallLog }
        if ($RuntimeExitCode -ne 0) { throw 'Could not prepare pip. Check runtime\install.log.' }
        $PipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $Requirements)
        $WheelsDir = Join-Path $ProjectDir 'wheels'
        if (Test-Path -LiteralPath $WheelsDir) {
            Invoke-Native { & $PythonExe @PipArgs --no-index --find-links $WheelsDir *>> $InstallLog }
            if ($RuntimeExitCode -ne 0) { Invoke-Native { & $PythonExe @PipArgs --find-links $WheelsDir *>> $InstallLog } }
        } else {
            Invoke-Native { & $PythonExe @PipArgs *>> $InstallLog }
        }
        if ($RuntimeExitCode -ne 0) { throw 'Dependency install failed. Check your network, then run run.bat again (see runtime\install.log).' }
        Invoke-Native { & $PythonExe -m pip check *>> $InstallLog }
        if ($RuntimeExitCode -ne 0) { throw 'Dependency check failed. Check runtime\install.log.' }
        [IO.File]::WriteAllText($DependencyStamp, $Fingerprint)
    }
    Install-OptionalPackages
    Write-Host '  - Starting local service...'
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
        throw 'The local service did not start. Check runtime\server.stderr.log.'
    }
    Write-Host ''
    Write-Host '  Ready: http://127.0.0.1:8756/' -ForegroundColor Green
    Write-Host '  The service keeps running in the background after this window closes.'
    if (-not $env:WXMOMENTS_NO_BROWSER) {
        $Stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        try { Start-Process "http://127.0.0.1:8756/?t=$Stamp" } catch { }
    }
    exit 0
} catch {
    Show-StartupError $_.Exception.Message @($InstallLog, $ServerOutLog, $ServerErrorLog)
    exit 1
}
